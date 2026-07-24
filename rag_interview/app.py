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
    "You are a VLSI physical-design assistant deciding whether a user's question "
    "genuinely needs the user to disambiguate before it can be answered well, "
    "given the lab sections retrieved for it.\n"
    "You are told, per FACET, the distinct values the retrieved sections span:\n"
    "  - provider: the EDA tool vendor (Synopsys, Cadence, Siemens/Calibre).\n"
    "  - stage: the flow stage (Synthesis, LEC, PnR, PV, STA).\n"
    "Decide with an engineer's judgment:\n"
    "  - Use your OWN knowledge to dismiss a FALSE spread. If the question is "
    "about CTS, floorplan, placement, routing or chip finish, the stage is "
    "obviously PnR even if a stray Synthesis section was retrieved — do NOT ask "
    "about stage. DRC/LVS/antenna/fill are PV; equivalence is LEC; etc.\n"
    "  - If the user already named the tool or stage — including via a tool name "
    "(ICC2/Design Compiler/PrimeTime/Formality = Synopsys; Innovus/Genus/Tempus/"
    "Conformal = Cadence; Calibre = Siemens) — that facet is settled; do NOT ask "
    "it.\n"
    "  - If the question asks to LIST or COUNT labs, explain a concept, or is "
    "about a whole stage or the whole course, it is NOT ambiguous — answer it; do "
    "NOT ask.\n"
    "  - Only ask when picking one value would genuinely change the answer (e.g. "
    "the exact command/flow differs between Synopsys and Cadence for the SAME "
    "task the user asked about).\n"
    "Return STRICT JSON: {\"clarify\": bool, \"facet\": \"provider\"|\"stage\"|"
    "null, \"question\": string, \"options\": [labels]}. When clarify=true, set "
    "facet to the ONE facet to disambiguate and options to the human labels for "
    "its values EXACTLY as given to you. When clarify=false, question=\"\" and "
    "options=[]."
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


# Facets whose value materially changes the answer. Each entry:
# (facet_key, label_key, default_question_text). doc_type (guided vs challenge)
# is deliberately NOT a clarify facet — the guided page reliably carries the
# commands, so we answer instead of asking.
_CLARIFY_FACETS = (
    ("stage", "stage_label", "Which flow/stage do you mean?"),
    ("provider", "provider_label", "Which tool do you mean?"),
)
_FACET_Q = {fk: q for fk, _lk, q in _CLARIFY_FACETS}


def _facet_spread(hits):
    """Deterministic DATA (not a heuristic): for each clarify facet, the distinct
    {code: label} the retrieved hits actually span. Only facets with 2+ distinct
    values are returned — those are the only ones there could be a choice about."""
    spread = {}
    for fk, lk, _q in _CLARIFY_FACETS:
        vals = {}
        for h in hits:
            code = (h.get("facets") or {}).get(fk)
            if code and code not in vals:
                vals[code] = (h.get("facets") or {}).get(lk) or code
        if len(vals) >= 2:
            spread[fk] = vals
    return spread


def _clarify_decision(question, hits):
    """LLM judgment: does this question genuinely need the user to disambiguate a
    facet before we can answer well? The facet SPREAD (what the hits actually
    span) is computed deterministically and handed to the model, which applies
    engineering judgment — dismissing false spreads it can resolve itself (CTS is
    obviously PnR), skipping facets the user already named, and never asking for
    list/count/concept questions. Returns a facet-shaped clarify dict (so the
    picked option still hard-restricts the corpus on the round-trip), or None."""
    spread = _facet_spread(hits)
    if not spread:
        return None                       # hits agree on every facet → no choice

    spread_desc = "\n".join(
        f"- {fk}: " + ", ".join(sorted(vals.values()))
        for fk, vals in spread.items())
    topics = "\n".join(
        f"  · [{(h.get('facets') or {}).get('stage_label', '?')}/"
        f"{(h.get('facets') or {}).get('provider_label', '?')}] "
        f"{h['lab_name']} :: {h['heading']}"
        for h in hits[:6])
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL, temperature=0, max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLARIFY_PROMPT},
                {"role": "user", "content":
                    f"Question: {question}\n\n"
                    f"Facets the retrieved sections span:\n{spread_desc}\n\n"
                    f"Retrieved section topics:\n{topics}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception:
        return None
    if not data.get("clarify"):
        return None
    fk = data.get("facet")
    if fk not in spread:                  # model picked a facet that isn't split
        return None
    vals = spread[fk]                     # {code: label}
    # Map the model's chosen labels back to REAL codes; drop anything invented.
    by_label = {str(l).lower(): c for c, l in vals.items()}
    codes = []
    for o in data.get("options", []):
        c = by_label.get(str(o).strip().lower())
        if c and c not in codes:
            codes.append(c)
    if len(codes) < 2:                    # fall back to all real values for facet
        codes = list(vals.keys())
    return {
        "question": data.get("question") or _FACET_Q[fk],
        "options": [vals[c] for c in codes],
        "facet": fk,
        "option_values": codes,
    }


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


# ---------------------------------------------------------------------------
# Module-order routing for "what's the next lab?" questions.
#
# "Which lab comes after X" is a CURRICULUM question — its answer is the fixed
# course order, not anything inside a testcase. Left to normal retrieval it's
# unreliable: a testcase's own "Next Step" section (meaning the next step WITHIN
# that lab) can outrank the right content, so the same question flips between
# "next lab = Floorplan" and "next = the challenge testcase". We answer it
# deterministically from the ordered curriculum below (mirrors the
# available-now modules of pd_learning_path.html, in stage order). Tool-agnostic
# — the module order is identical for Synopsys and Cadence — so no tool clarify.
# ---------------------------------------------------------------------------
_CURRICULUM = [
    ("SYN", "Synthesis", "Unresolved References"),
    ("SYN", "Synthesis", "Check Timing Issues"),
    ("SYN", "Synthesis", "Logic Aware Synthesis"),
    ("SYN", "Synthesis", "Physical Aware Synthesis"),
    ("LEC", "LEC", "RTL to Netlist"),
    ("LEC", "LEC", "Netlist to Netlist"),
    ("LEC", "LEC", "Clock Gating App Option"),
    ("PNR", "PnR", "Design Init"),
    ("PNR", "PnR", "Floorplan"),
    ("PNR", "PnR", "Placement"),
    ("PNR", "PnR", "CTS"),
    ("PNR", "PnR", "Route"),
    ("PNR", "PnR", "Post Route"),
    ("PNR", "PnR", "Chip Finish"),
    ("PV", "PV", "DRC Flow"),
    ("PV", "PV", "LVS Flow"),
    ("PV", "PV", "Antenna"),
    ("PV", "PV", "Dummy Fill FEOL"),
    ("PV", "PV", "Dummy Fill BEOL"),
    ("PV", "PV", "GDS Merging"),
    ("STA", "STA", "STA Flow"),
    ("STA", "STA", "DMSA Based ECO Generation"),
]
_STAGE_NUM = {"SYN": 1, "LEC": 2, "PNR": 3, "PV": 4, "STA": 5}

# Alias patterns per module. Matching picks the LONGEST match so "post route"
# resolves to Post Route rather than Route.
_MODULE_ALIASES = {
    "Unresolved References": r"unresolved\s+ref\w*|missing\s+ref\w*|unresolved",
    "Check Timing Issues":   r"check\s+timing|timing\s+issue\w*",
    "Logic Aware Synthesis": r"logic[\s-]*aware(?:\s+synth\w*)?",
    "Physical Aware Synthesis": r"physical[\s-]*aware(?:\s+synth\w*)?",
    "RTL to Netlist":  r"rtl[\s-]*to[\s-]*netlist|rtl2netlist|\br2n\b",
    "Netlist to Netlist": r"netlist[\s-]*to[\s-]*netlist|\bn2n\b",
    "Clock Gating App Option": r"clock[\s-]*gating(?:\s+app\w*)?",
    "Design Init":  r"design\s*init\w*|design\s+initial\w*|initializ\w*|\bdi\b|\binit\b",
    "Floorplan":    r"floor[\s-]*plan\w*|\bfp\b",
    "Placement":    r"placement|\bplacing\b|\bplace\b|\bpl\b",
    "CTS":          r"\bcts\b|clock\s+tree(?:\s+synth\w*)?",
    "Route":        r"\brout(?:e|ing)\b|detail\s+rout\w*|\brt\b",
    "Post Route":   r"post[\s-]*rout\w*",
    "Chip Finish":  r"chip\s*finish\w*|\bcf\b",
    "DRC Flow":     r"\bdrc\b",
    "LVS Flow":     r"\blvs\b",
    "Antenna":      r"antenna",
    "Dummy Fill FEOL": r"dummy\s*fill\s*feol|feol\s*(?:fill|dummy)|\bfeol\b",
    "Dummy Fill BEOL": r"dummy\s*fill\s*beol|beol\s*(?:fill|dummy)|\bbeol\b",
    "GDS Merging":  r"gds\s*merg\w*|merg\w*\s*gds",
    "STA Flow":     r"\bsta\s*flow\b|static\s+timing(?:\s+analysis)?",
    "DMSA Based ECO Generation": r"\bdmsa\b|eco\s+generation",
}

_NEXT_INTENT_RE = re.compile(
    r"\b(next|after|following|subsequent|proceed|onwards?)\b"
    r"|what\s+now|now\s+what|move\s+on|where\s+to\b", re.I)
# If the question is command/step scoped, it isn't a curriculum "next lab" ask.
_CMD_SCOPE_RE = re.compile(
    r"\b(command|cmd|script|run|execute|syntax|option|flag|tcl|step)\b", re.I)


def _match_module(text):
    """Return the canonical module named in text (longest alias match), or None."""
    best = None  # (match_len, canonical_name)
    for canon, pat in _MODULE_ALIASES.items():
        m = re.search(pat, text, re.I)
        if m and (best is None or (m.end() - m.start()) > best[0]):
            best = (m.end() - m.start(), canon)
    return best[1] if best else None


def _stage_order(stage_code):
    """Ordered module names of one stage, for showing the sequence."""
    return [m for (sc, _, m) in _CURRICULUM if sc == stage_code]


def _next_lab_route(question):
    """If the question asks which lab/module comes next after a named module,
    answer deterministically from _CURRICULUM. Else None (fall through to RAG).
    """
    if _CMD_SCOPE_RE.search(question) or not _NEXT_INTENT_RE.search(question):
        return None
    module = _match_module(question)
    if not module:
        return None
    idx = next((i for i, (_, _, m) in enumerate(_CURRICULUM) if m == module), None)
    if idx is None:
        return None

    sc, slabel, mod = _CURRICULUM[idx]
    snum = _STAGE_NUM[sc]
    order = " → ".join(_stage_order(sc))
    if idx == len(_CURRICULUM) - 1:
        answer = (
            f"You've completed **{mod}** — the final lab in the course "
            f"(Stage {snum}, {slabel}). That's the end of the physical-design "
            f"learning path. 🎉\n\n{slabel} order: {order}")
    else:
        nsc, nslabel, nmod = _CURRICULUM[idx + 1]
        if nsc == sc:
            answer = (
                f"You finished **{mod}**. The next lab in **Stage {snum} — "
                f"{slabel}** is **{nmod}**.\n\n{slabel} order: {order}\n\n"
                f"Each module has a **Guided** lab (step-by-step) then a "
                f"**Challenge** lab (open-ended), so do {nmod} Guided next.")
        else:
            nnum = _STAGE_NUM[nsc]
            answer = (
                f"You finished **{mod}**, the last lab in **Stage {snum} — "
                f"{slabel}**. Next is **Stage {nnum} — {nslabel}**, starting "
                f"with **{nmod}**.\n\n{nslabel} order: "
                f"{' → '.join(_stage_order(nsc))}")

    source = {
        "n": 1,
        "lab_name": "Recommended Learning Path",
        "heading": f"Stage {snum} — {slabel}",
        "source": OVERVIEW_SOURCE,
        "score": 1.0,
        "facets": {},
        "content": f"{slabel} modules, in order: {order}",
    }
    return {"answer": answer, "sources": [source]}


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
    # 0a. "Which lab comes after X?" is a curriculum-order question — answer it
    #     deterministically from the course sequence before any retrieval, so it
    #     never gets hijacked by a testcase's internal "Next Step" section.
    routed = _next_lab_route(req.question)
    if routed:
        return routed

    # 0b. If the question already names a vendor-specific tool (e.g. ICC2 =
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

    # 2. Ambiguity check — a single LLM judgment call. The learning-path overview
    #    page carries no facets; a question is "catalog-level" when that page is
    #    the strongest hit OR it asks for one stage's ordered labs (routed by
    #    _stage_catalog even if a specific lab ranks #1). Catalog questions are
    #    answered from the whole overview page — which already covers every tool
    #    and stage — so they never need disambiguation. Only lab-content questions
    #    with confident retrieval go through the clarify judgment, which decides
    #    (using the real facet spread of the hits) whether a tool/stage choice
    #    genuinely changes the answer. The picked option comes back as a facet, so
    #    it hard-restricts the corpus on the follow-up request.
    top_is_overview = not (hits[0].get("facets") or {})
    overview_in_hits = any(not (h.get("facets") or {}) for h in hits)
    stage_catalog = _stage_catalog(req.question) \
        if (req.allow_clarify and overview_in_hits) else None
    overview_mode = top_is_overview or stage_catalog is not None

    if req.allow_clarify and not overview_mode \
            and hits[0]["score"] >= MIN_CLARIFY_SCORE:
        clar = _clarify_decision(req.question, hits)
        if clar:
            return {
                "clarify": True,
                "question": clar["question"],
                "options": clar["options"],
                "facet": clar["facet"],
                "option_values": clar["option_values"],
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
