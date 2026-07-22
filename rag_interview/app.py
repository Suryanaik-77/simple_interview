"""
Standalone RAG application over the lab-testcase HTML content.

A small FastAPI app: the user asks a question, the engine retrieves the most
relevant lab sections, and an LLM answers grounded in those sections (with
citations). Completely independent of the main interview platform.

Run:
    cd rag_interview
    uvicorn app:app --reload --port 8100
Then open http://localhost:8100
"""

import os
import re
import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel
from openai import OpenAI

from rag_engine import RAGEngine, sanitize_paths, parse_facets

_HERE = os.path.dirname(os.path.abspath(__file__))

# Chat/verify run on DeepSeek V4 Flash (OpenAI-compatible API). Embeddings stay
# on OpenAI inside rag_engine — DeepSeek has no embeddings endpoint.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CHAT_MODEL = "deepseek-v4-flash"     # final answer
VERIFY_MODEL = "deepseek-v4-flash"   # small verifier agent
RETRIEVE_K = 8                       # retrieve wide, then verify + expand
# Only offer a clarify when retrieval is actually confident. Legit lab questions
# score ~0.55-0.65 cosine; off-topic/nonsense scores <0.2. Below this we skip
# clarify and let verify/answer say the content isn't present.
MIN_CLARIFY_SCORE = 0.40

app = FastAPI(title="Lab RAG")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

# Build/load the index once at startup.
engine = RAGEngine.load_or_build()
_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=DEEPSEEK_BASE_URL,
    timeout=60.0,
    max_retries=2,
)

SYSTEM_PROMPT = (
    "You are a precise VLSI engineer assisting with lab testcases. Follow these "
    "rules:\n"
    "1. Answer using ONLY the provided context sections — never outside "
    "knowledge.\n"
    "2. If the user names a specific lab/testcase, only use sections from that "
    "same lab. If none of the context is from the lab they asked about, reply "
    "that the information is not available for that lab.\n"
    "3. Do NOT accept a false premise. If the context does not support what the "
    "question assumes (e.g. a file or step that isn't there), say it is not "
    "present rather than describing it from general knowledge.\n"
    "4. Be concise and technical. Cite facts as [1], [2] matching the numbered "
    "context blocks. (A separate step already asks the user to disambiguate when "
    "needed, so assume the question is as specific as given and answer it.)"
)

# When a question could apply to several tools/flows/testcases, a good engineer
# asks which one the user means instead of guessing. This agent decides that and
# returns concrete options the UI renders as clickable buttons.
CLARIFY_PROMPT = (
    "You are a VLSI engineer deciding whether a user's question is too ambiguous "
    "to answer well from the retrieved lab sections. It is ambiguous when the "
    "sections span DISTINCT options the user has not chosen between:\n"
    "  - different tool / provider (Synopsys Formality vs Cadence Conformal),\n"
    "  - different flow or stage (RTL-to-Gate vs Gate-to-Gate LEC),\n"
    "  - different testcase (guided vs challenge; AES vs CVA6 vs Iguana), or\n"
    "  - a generic concept explanation vs step-by-step commands for one "
    "testcase.\n"
    "Return STRICT JSON: {\"clarify\": bool, \"question\": string, "
    "\"options\": [string, ...]}. Set clarify=true ONLY when choosing one option "
    "materially changes the answer AND the user did not already specify it. Give "
    "a short question and 2-5 concrete, short option labels drawn from the ACTUAL "
    "sections (e.g. \"Gate-to-Gate LEC (Iguana challenge)\"), each pickable in "
    "one click. If the question is already specific, names a testcase, or only "
    "one option fits, set clarify=false with an empty options list."
)

# Whether to ask the student "Synopsys or Cadence?" before listing a stage's
# labs is split into a FACT and an INTENT part. The fact — which stages offer a
# real tool choice — is read deterministically from the corpus (a stage with
# 2+ providers present). The intent — is the user actually asking for a single
# stage's ordered labs (vs. the overall flow, or where to begin) — is the one
# fuzzy bit, judged by a narrow LLM yes/no. (An earlier open-ended "does this
# need a tool?" prompt wobbled: the model kept reasoning "the modules are the
# same in both tools, so no" — true, but it defeats the point of asking.)
STAGE_INTENT_PROMPT = (
    "A student is browsing a physical-design course whose flow has stages "
    "(Synthesis, LEC, PnR, PV, STA). Decide whether their question asks for the "
    "concrete labs or the ordered module path WITHIN one specific stage — the "
    "labs they would actually run — as opposed to a general question about the "
    "overall flow, the list of stages, where to begin the course, or why the "
    "order is what it is.\n"
    "Return STRICT JSON: {\"labs_of_stage\": bool}. Example true: 'give me the "
    "synthesis stage labs in order'. Example false: 'if I already know "
    "synthesis, where should I start?' (that asks about the entry point)."
)

# The corpus is already path-sanitized at ingest (rag_engine.sanitize_paths),
# so the index is clean. We still sanitize LLM outputs as defense-in-depth in
# case a model ever echoes a central path from its own priors.
_sanitize_paths = sanitize_paths


VERIFY_PROMPT = (
    "You select which retrieved sections are actually relevant to the user's "
    "question AND judge whether they are enough to answer it. Consider the "
    "specific lab/testcase the user names. Return STRICT JSON: "
    "{\"relevant\": [<indices>], \"sufficient\": <bool>}.\n"
    "- relevant: include an index only if that section genuinely helps answer "
    "THIS question for the lab asked about. If the user names a lab and a "
    "section is from a different lab, exclude it. If nothing is relevant, "
    "return an empty list.\n"
    "- sufficient: true ONLY if the relevant sections above already contain "
    "everything needed to answer the question completely and specifically "
    "(exact command, value, file, or steps asked for). Set it false if they "
    "are only partially on-topic, look truncated, or hint at the answer "
    "without stating it — those need the full section pulled in."
)


class AskRequest(BaseModel):
    question: str
    lab_name: str | None = None
    k: int = 5
    allow_clarify: bool = True   # False once the user has picked an option
    facets: dict | None = None   # {"provider": "SNPS"} once a facet is picked


def _verify_relevant(question, hits):
    """Small agent: pick indices of relevant hits and judge sufficiency.

    Returns (relevant_indices, sufficient). `sufficient` is True when those
    sub-chunks already contain enough to answer completely — the caller then
    skips full-section expansion. When False, the caller expands the winning
    chunks to their full sections before answering.
    """
    # Give the verifier the full sub-chunk (not a 220-char preview) so its
    # sufficiency judgement is made on the actual retrieved text.
    listing = "\n".join(
        f"{i}. [lab: {h['lab_name']} | section: {h['heading']}] "
        f"{_sanitize_paths(h['content'])}"
        for i, h in enumerate(hits)
    )
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL,
            temperature=0,
            # v4-flash thinks first (reasoning_content), then emits the JSON in
            # content — budget enough tokens for both.
            max_tokens=1024,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": VERIFY_PROMPT},
                {"role": "user",
                 "content": f"Question: {question}\n\nSections:\n{listing}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        idxs = [int(i) for i in data.get("relevant", [])
                if isinstance(i, (int, float)) and 0 <= int(i) < len(hits)]
        sufficient = bool(data.get("sufficient", False))
        return idxs, sufficient
    except Exception:
        # On any failure, fall back to the top-3 retrieved chunks and expand
        # them (treat as insufficient) so the answer gets full context.
        return list(range(min(3, len(hits)))), False


# Facets whose value materially changes the answer, in the order we'd ask about
# them (coarsest first). Each entry: (facet_key, label_key, question_text).
# NOTE: doc_type (guided vs challenge vs overview) is deliberately NOT a clarify
# facet — nearly every topic has all three, so gating on it asks the user
# "guided or challenge?" on almost every question. The guided page reliably
# carries the commands, so we just answer instead of asking.
_CLARIFY_FACETS = (
    ("stage", "stage_label", "Which flow/stage do you mean?"),
    ("provider", "provider_label", "Which tool do you mean?"),
)


def _facet_clarify(question, hits, only=None):
    """Deterministic clarify from real filename facets — no LLM guessing.

    If the retrieved hits span more than one value of a materially-different
    facet (stage / provider / guided-vs-challenge) that the question did NOT
    already specify, return that facet's real values as clickable options.
    `only` optionally restricts which facets may be asked (e.g. ("provider",)
    for catalog-level questions, where a "which stage?" clarify is nonsensical
    but a Cadence-vs-Synopsys tool choice is still real).
    Returns {"question", "options", "facet", "option_values"} or None.
    """
    qlc = question.lower()
    for fk, lk, qtext in _CLARIFY_FACETS:
        if only and fk not in only:
            continue
        values = {}                       # code -> human label, first seen wins
        for h in hits:
            f = h.get("facets") or {}
            code = f.get(fk)
            if code and code not in values:
                values[code] = f.get(lk) or code
        if len(values) < 2:
            continue
        # Skip if the user already named one of these (by code or label).
        if any(code.lower() in qlc or str(lbl).lower() in qlc
               for code, lbl in values.items()):
            continue
        codes = list(values.keys())
        return {
            "question": qtext,
            "options": [values[c] for c in codes],
            "facet": fk,
            "option_values": codes,
        }
    return None


_PROVIDER_LABEL = {"SNPS": "Synopsys", "CDN": "Cadence", "SIE": "Siemens"}

# Tool PRODUCT names -> owning provider. This is vendor fact (like SNPS=Synopsys),
# not an interview heuristic: each tool belongs to exactly one EDA vendor. Names
# taken from the ones that actually appear in the corpus. When a question names
# one of these (e.g. "ICC2 floorplanning"), the tool is already specified, so a
# "Synopsys or Cadence?" clarify is redundant — we resolve it silently instead.
_PROVIDER_TOOL_RE = {
    "SNPS": re.compile(
        r"\bicc2\b|\bic\s*compiler\b|\bfusion\s*compiler\b|\bdesign\s*compiler\b"
        r"|\bprimetime\b|\bformality\b|\bstarrc\b|\bvcs\b", re.I),
    "CDN": re.compile(
        r"\binnovus\b|\bgenus\b|\btempus\b|\bconformal\b|\bvoltus\b|\bquantus\b",
        re.I),
    "SIE": re.compile(r"\bcalibre\b", re.I),
}


def _provider_from_tool(question):
    """Return the provider code uniquely implied by a tool name in the question,
    or None if zero or more than one vendor's tools are named (ambiguous)."""
    hit = [p for p, rx in _PROVIDER_TOOL_RE.items() if rx.search(question)]
    return hit[0] if len(hit) == 1 else None

# Which stages a question can name, matched case-insensitively with word
# boundaries (so "sta" doesn't fire on "stage"/"start", "pv" not on "provide").
_STAGE_PATTERNS = {
    "SYN": re.compile(r"\bsynthesis\b|\bsynth\b", re.I),
    "PNR": re.compile(r"\bpnr\b|\bp\s*&\s*r\b|place\s*(?:and|&)\s*route", re.I),
    "STA": re.compile(r"\bsta\b|static\s+timing|timing\s+analysis", re.I),
    "LEC": re.compile(r"\blec\b|equivalence|formality|conformal", re.I),
    "PV": re.compile(r"\bpv\b|physical\s+verification", re.I),
}


def _stage_providers():
    """{stage_code: {provider_codes}} present in the corpus — the ground truth
    for whether a stage offers a real tool choice."""
    sp = {}
    for c in engine.chunks:
        f = c.get("facets") or {}
        st, pr = f.get("stage"), f.get("provider")
        if st and pr:
            sp.setdefault(st, set()).add(pr)
    return sp


STAGE_PROVIDERS = _stage_providers()


def _asks_stage_labs(question):
    """Narrow LLM gate: is the user asking for a single stage's ordered labs?"""
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL,
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": STAGE_INTENT_PROMPT},
                {"role": "user", "content": f"Question: {question}"},
            ],
        )
        return bool(json.loads(resp.choices[0].message.content).get("labs_of_stage"))
    except Exception:
        return False


def _stage_catalog(question):
    """If the question asks for ONE specific stage's ordered labs, return that
    stage code — else None. Deterministic stage match + narrow LLM intent gate.
    Such questions are catalog questions answered from the learning-path page,
    even when a specific lab happens to rank #1 in retrieval.
    """
    named = [st for st, pat in _STAGE_PATTERNS.items() if pat.search(question)]
    if len(named) != 1:                     # need exactly one stage named
        return None
    if not _asks_stage_labs(question):      # e.g. "where do I start" -> not this
        return None
    return named[0]


def _stage_tool_clarify(stage):
    """Provider clarify for a stage that offers a real tool choice, else None."""
    codes = [p for p in ("SNPS", "CDN", "SIE")
             if p in STAGE_PROVIDERS.get(stage, set())]
    if len(codes) < 2:                      # stage is fixed to one tool
        return None
    return {
        "question": "Which tool do you mean?",
        "options": [_PROVIDER_LABEL.get(c, c) for c in codes],
        "facet": "provider",
        "option_values": codes,
    }


OVERVIEW_SOURCE = "pd_learning_path.html"


def _overview_doc_sections():
    """Every section of the learning-path page, in document order, fully
    reassembled. Catalog-level answers ground on the whole (tiny) page so they
    are complete — all five stages, each stage's modules in order — instead of
    a lossy top-k that can split a section at the chunk boundary and drop half
    the stages.
    """
    out, seen = [], set()
    for c in engine.chunks:
        if c["source"] != OVERVIEW_SOURCE:
            continue
        key = (c["lab_name"], c["heading"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "lab_name": c["lab_name"],
            "heading": c["heading"],
            "source": c["source"],
            "score": 1.0,
            "facets": c.get("facets") or {},
            "content": _sanitize_paths(
                engine.get_section(c["lab_name"], c["heading"])),
        })
    return out


def _clarify_needed(question, hits):
    """Return {"question", "options"} if the request is ambiguous, else None."""
    listing = "\n".join(
        f"{i}. [lab: {h['lab_name']} | section: {h['heading']}] "
        f"{_sanitize_paths(h['content'][:180])}"
        for i, h in enumerate(hits)
    )
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL,
            temperature=0,
            max_tokens=800,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLARIFY_PROMPT},
                {"role": "user",
                 "content": f"Question: {question}\n\nSections:\n{listing}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        if not data.get("clarify"):
            return None
        opts, seen = [], set()
        for o in data.get("options", []):
            label = _sanitize_paths(str(o)).strip()
            if label and label.lower() not in seen:
                seen.add(label.lower())
                opts.append(label)
        opts = opts[:5]
        if len(opts) < 2:            # nothing meaningful to choose between
            return None
        q = _sanitize_paths(str(data.get("question") or "").strip()) \
            or "Which one do you mean?"
        return {"question": q, "options": opts}
    except Exception:
        # On any failure, don't block — fall through to a normal answer.
        return None


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "rag.html", {"request": request, "labs": engine.labs}
    )


@app.get("/api/labs")
def api_labs():
    return {"labs": engine.labs, "chunks": len(engine.chunks)}


@app.post("/api/ask")
def api_ask(req: AskRequest):
    # 0. If the question already names a vendor-specific tool (e.g. ICC2 =
    #    Synopsys, Innovus = Cadence), the tool is settled — resolve the provider
    #    silently so retrieval restricts to that vendor and we skip a redundant
    #    "Synopsys or Cadence?" clarify. Only when the user hasn't picked one.
    if not (req.facets or {}).get("provider"):
        implied = _provider_from_tool(req.question)
        if implied:
            req.facets = {**(req.facets or {}), "provider": implied}

    # 1. Retrieve wide (precise sub-chunks). A picked facet hard-restricts.
    hits = engine.search(req.question, k=max(req.k, RETRIEVE_K),
                         lab_name=req.lab_name, facets=req.facets)
    if not hits:
        return {"answer": "No lab content is indexed.", "sources": []}

    # 2. Ambiguity check. Prefer DETERMINISTIC facet clarify (real Synopsys vs
    #    Cadence / stage / guided-vs-challenge values from the filenames); only
    #    fall back to the LLM clarify for subtler within-facet ambiguity. The UI
    #    renders the options as buttons.
    # The learning-path / lab-catalog overview page carries no facets (it's
    # tool- and stage-agnostic). A question is "catalog-level" when either that
    # page is the strongest hit OR it asks for one stage's ordered labs (routed
    # by _stage_catalog even if a specific lab ranks #1 — retrieval order is
    # phrasing-sensitive). We only probe _stage_catalog when the overview page
    # is actually retrieved, so specific lab questions pay no extra LLM call.
    top_is_overview = not (hits[0].get("facets") or {})
    overview_in_hits = any(not (h.get("facets") or {}) for h in hits)
    already_picked_tool = bool((req.facets or {}).get("provider"))
    stage_catalog = _stage_catalog(req.question) \
        if (req.allow_clarify and overview_in_hits) else None
    overview_mode = top_is_overview or stage_catalog is not None

    if req.allow_clarify and (overview_mode or hits[0]["score"] >= MIN_CLARIFY_SCORE):
        # Catalog-level question: a "which stage?" clarify is nonsensical, but a
        # Cadence-vs-Synopsys choice is real when the asked-about stage offers
        # both tools (e.g. "give me the synthesis labs in order"). Testcase-led
        # question: use the deterministic filename facets.
        if overview_mode:
            clar = _stage_tool_clarify(stage_catalog) \
                if (stage_catalog and not already_picked_tool) else None
        else:
            clar = _facet_clarify(req.question, hits)
        if clar:
            return {
                "clarify": True,
                "question": clar["question"],
                "options": clar["options"],
                "facet": clar["facet"],
                "option_values": clar["option_values"],
                "sources": [],
            }
        # LLM clarify only for MEANINGFUL within-provider variant ambiguity
        # (e.g. logic- vs physical-aware synthesis) — NOT guided-vs-challenge of
        # the same variant, which we answer directly from the guided page.
        variants = {(h.get("facets") or {}).get("variant") for h in hits}
        variants.discard(None)
        if not overview_mode and len(variants) > 1:
            clar = _clarify_needed(req.question, hits)
            if clar:
                return {
                    "clarify": True,
                    "question": clar["question"],
                    "options": clar["options"],
                    "sources": [],
                }

    # 3. Overview-led catalog question: ground on the WHOLE (tiny) learning-path
    #    page, in document order, so the answer is complete. Skips the top-k
    #    verifier/expander, which can split a section at the chunk boundary and
    #    drop half the stages (e.g. only 3 of the 5 stages surviving).
    fallback = False
    if overview_mode:
        sections = _overview_doc_sections()
    else:
        # Verifier agent: keep the sub-chunks that truly match AND judge whether
        # they already suffice to answer.
        keep, sufficient = _verify_relevant(req.question, hits)
        # If nothing passes strict verification, don't dead-end. Fall back to the
        # closest retrieved sections (still ONLY lab content, already restricted
        # to the chosen lab/tool) so the answer can say what ISN'T there AND
        # point to the lab's actual equivalent — e.g. "Synopsys has no clock-tree
        # spec file; CTS there is run via clock_opt." The prompt keeps it honest.
        if not keep:
            keep = list(range(min(3, len(hits))))
            fallback = True
            sufficient = False  # nothing verified -> always expand for context

        # 4. Build the context sections. If the verified sub-chunks are already
        #    sufficient, use them as-is (cheaper, tighter). If NOT sufficient,
        #    expand each winning sub-chunk back to its FULL section (all sibling
        #    chunks sharing the same lab+heading) for more context.
        sections, seen = [], set()
        for i in keep:
            h = hits[i]
            # When expanding, all sub-chunks of one section resolve to the same
            # full section, so de-dup by (lab, heading). When the sub-chunks are
            # used as-is, each is distinct content, so de-dup by chunk text.
            key = (h["lab_name"], h["heading"]) if not sufficient else h["content"]
            if key in seen:
                continue
            seen.add(key)
            content = h["content"] if sufficient else engine.get_section(
                h["lab_name"], h["heading"])
            sections.append({
                "lab_name": h["lab_name"],
                "heading": h["heading"],
                "source": h["source"],
                "score": round(h["score"], 3),
                "facets": h.get("facets") or {},
                "content": _sanitize_paths(content),
            })

        # Cross-tool probe: when nothing in-scope verified, the asked-about
        # command/term may simply belong to the OTHER vendor's flow (e.g.
        # create_clock_tree_spec is Cadence Innovus; Synopsys ICC2 builds the
        # tree directly via clock_opt). If an unfiltered search ranks another
        # provider's chunk ABOVE every in-scope hit, include that full section
        # as clearly-labelled other-tool context so the answer can name the
        # right tool instead of dead-ending on a bare "not present".
        picked = (req.facets or {}).get("provider")
        if fallback and picked:
            xhits = engine.search(req.question, k=1, lab_name=req.lab_name)
            x = xhits[0] if xhits else None
            xfac = (x.get("facets") or {}) if x else {}
            xprov = xfac.get("provider")
            # Only surface the OTHER vendor's chunk when it's the same STAGE —
            # i.e. that tool's version of the SAME task (CTS Synopsys vs CTS
            # Cadence), not an unrelated stage that merely out-scores a weak
            # in-scope hit (e.g. a Siemens PV antenna check for a CTS question).
            tstage = (hits[0].get("facets") or {}).get("stage")
            if (x and xprov and xprov != picked
                    and xfac.get("stage") and xfac.get("stage") == tstage
                    and x["score"] > hits[0]["score"]):
                sections.append({
                    "lab_name": x["lab_name"],
                    "heading": x["heading"],
                    "source": x["source"],
                    "score": round(x["score"], 3),
                    "facets": x.get("facets") or {},
                    "other_tool": _PROVIDER_LABEL.get(xprov, xprov),
                    "content": _sanitize_paths(
                        engine.get_section(x["lab_name"], x["heading"])),
                })

    # 5. Final answer from the reassembled full sections.
    context = "\n\n".join(
        f"[{n + 1}] ({s['lab_name']} :: {s['heading']})"
        + (f" — NOTE: this section is from the {s['other_tool']} flow, NOT the "
           "tool the user chose" if s.get("other_tool") else "")
        + f"\n{s['content']}"
        for n, s in enumerate(sections)
    )
    other_tool = next((s["other_tool"] for s in sections if s.get("other_tool")),
                      None)
    fallback_note = (
        "\n\nNote: no section directly matches the question. If these sections "
        "do not contain the exact command/term asked about, say plainly that it "
        "is not part of this lab/tool, then briefly state what this lab uses "
        "instead — using ONLY these sections, never outside knowledge."
        + (f" The section marked as from the {other_tool} flow shows the "
           f"asked-about item DOES exist there: say in one sentence that it "
           f"belongs to the {other_tool} flow and name the exact command from "
           f"that section, but do not present {other_tool} steps as part of "
           "the chosen tool's flow." if other_tool else "")
    ) if fallback else ""
    resp = _client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0.2,
        # Room for v4-flash reasoning tokens plus the final grounded answer.
        max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Context sections:\n\n{context}\n\n"
                        f"Question: {req.question}{fallback_note}"},
        ],
    )
    answer = _sanitize_paths(resp.choices[0].message.content)

    sources = [{"n": n + 1, **s} for n, s in enumerate(sections)]
    return {"answer": answer, "sources": sources}


@app.get("/health")
def health():
    return {"status": "ok", "chunks": len(engine.chunks), "labs": len(engine.labs)}
