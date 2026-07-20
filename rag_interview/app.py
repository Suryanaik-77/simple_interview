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


def _facet_clarify(question, hits):
    """Deterministic clarify from real filename facets — no LLM guessing.

    If the retrieved hits span more than one value of a materially-different
    facet (stage / provider / guided-vs-challenge) that the question did NOT
    already specify, return that facet's real values as clickable options.
    Returns {"question", "options", "facet", "option_values"} or None.
    """
    qlc = question.lower()
    for fk, lk, qtext in _CLARIFY_FACETS:
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
    # 1. Retrieve wide (precise sub-chunks). A picked facet hard-restricts.
    hits = engine.search(req.question, k=max(req.k, RETRIEVE_K),
                         lab_name=req.lab_name, facets=req.facets)
    if not hits:
        return {"answer": "No lab content is indexed.", "sources": []}

    # 2. Ambiguity check. Prefer DETERMINISTIC facet clarify (real Synopsys vs
    #    Cadence / stage / guided-vs-challenge values from the filenames); only
    #    fall back to the LLM clarify for subtler within-facet ambiguity. The UI
    #    renders the options as buttons.
    if req.allow_clarify and hits[0]["score"] >= MIN_CLARIFY_SCORE:
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
        if len(variants) > 1:
            clar = _clarify_needed(req.question, hits)
            if clar:
                return {
                    "clarify": True,
                    "question": clar["question"],
                    "options": clar["options"],
                    "sources": [],
                }

    # 3. Verifier agent: keep the sub-chunks that truly match AND judge whether
    #    they already suffice to answer.
    keep, sufficient = _verify_relevant(req.question, hits)
    # If nothing passes strict verification, don't dead-end. Fall back to the
    # closest retrieved sections (still ONLY lab content, already restricted to
    # the chosen lab/tool) so the answer can say what ISN'T there AND point to
    # the lab's actual equivalent — e.g. "Synopsys has no clock-tree spec file;
    # CTS there is run via clock_opt." The prompt keeps it honest.
    fallback = False
    if not keep:
        keep = list(range(min(3, len(hits))))
        fallback = True
        sufficient = False  # nothing verified -> always expand for full context

    # 4. Build the context sections. If the verified sub-chunks are already
    #    sufficient, use them as-is (cheaper, tighter). If NOT sufficient, expand
    #    each winning sub-chunk back to its FULL section (all sibling chunks
    #    sharing the same lab+heading) to give the answer more context.
    sections, seen = [], set()
    for i in keep:
        h = hits[i]
        # When expanding, all sub-chunks of one section resolve to the same full
        # section, so de-dup by (lab, heading). When the sub-chunks are used
        # as-is, each is distinct content, so de-dup by the chunk text instead.
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

    # 5. Final answer from the reassembled full sections.
    context = "\n\n".join(
        f"[{n + 1}] ({s['lab_name']} :: {s['heading']})\n{s['content']}"
        for n, s in enumerate(sections)
    )
    fallback_note = (
        "\n\nNote: no section directly matches the question. If these sections "
        "do not contain the exact command/term asked about, say plainly that it "
        "is not part of this lab/tool, then briefly state what this lab uses "
        "instead — using ONLY these sections, never outside knowledge."
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
