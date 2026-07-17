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

from rag_engine import RAGEngine

_HERE = os.path.dirname(os.path.abspath(__file__))

# Chat/verify run on DeepSeek V4 Flash (OpenAI-compatible API). Embeddings stay
# on OpenAI inside rag_engine — DeepSeek has no embeddings endpoint.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CHAT_MODEL = "deepseek-v4-flash"     # final answer
VERIFY_MODEL = "deepseek-v4-flash"   # small verifier agent
RETRIEVE_K = 8                       # retrieve wide, then verify + expand

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

# Internal/central lab paths must never reach the candidate. Rewrite any
# central admin path to the user-space working-directory convention
# (~/pd/labs/...) wherever it appears — matching how every other lab is worded.
# Applied to the verifier's section listing (the middle chunk-verification
# model), to the answer context, and to the returned answer + sources, so no
# central path can leak even from future lab data.
_CENTRAL_PATH_RES = (
    (re.compile(r"/proj5/semicon_labs/central_labs/labs/"), "~/pd/labs/"),
    (re.compile(r"/proj5/semicon_labs/central_labs/"), "~/pd/labs/"),
)


def _sanitize_paths(text):
    """Replace central/admin lab paths with the user-facing ~/pd/labs/ path."""
    if not text:
        return text
    for pattern, repl in _CENTRAL_PATH_RES:
        text = pattern.sub(repl, text)
    return text


VERIFY_PROMPT = (
    "You select which retrieved sections are actually relevant to the user's "
    "question. Consider the specific lab/testcase the user names. Return STRICT "
    "JSON: {\"relevant\": [<indices>]}. Include an index only if that section "
    "genuinely helps answer THIS question for the lab asked about. If the user "
    "names a lab and a section is from a different lab, exclude it. If nothing "
    "is relevant, return an empty list."
)


class AskRequest(BaseModel):
    question: str
    lab_name: str | None = None
    k: int = 5
    allow_clarify: bool = True   # False once the user has picked an option


def _verify_relevant(question, hits):
    """Small agent: pick indices of hits that truly answer the question."""
    listing = "\n".join(
        f"{i}. [lab: {h['lab_name']} | section: {h['heading']}] "
        f"{_sanitize_paths(h['content'][:220])}"
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
        return idxs
    except Exception:
        # On any failure, fall back to the top-3 retrieved chunks.
        return list(range(min(3, len(hits))))


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
    # 1. Retrieve wide (precise sub-chunks).
    hits = engine.search(req.question, k=max(req.k, RETRIEVE_K),
                         lab_name=req.lab_name)
    if not hits:
        return {"answer": "No lab content is indexed.", "sources": []}

    # 2. If the hits span multiple labs, the question may be ambiguous — ask the
    #    user to pick instead of guessing. The UI renders options as buttons.
    if req.allow_clarify and len({h["lab_name"] for h in hits}) > 1:
        clar = _clarify_needed(req.question, hits)
        if clar:
            return {
                "clarify": True,
                "question": clar["question"],
                "options": clar["options"],
                "sources": [],
            }

    # 3. Verifier agent: keep only the sub-chunks that truly match.
    keep = _verify_relevant(req.question, hits)
    if not keep:
        return {
            "answer": "I don't have that information in the lab content for "
                      "the lab you asked about.",
            "sources": [],
        }

    # 4. Expand each winning sub-chunk back to its FULL section (all sibling
    #    chunks sharing the same lab+heading), de-duplicated in order.
    sections, seen = [], set()
    for i in keep:
        h = hits[i]
        key = (h["lab_name"], h["heading"])
        if key in seen:
            continue
        seen.add(key)
        sections.append({
            "lab_name": h["lab_name"],
            "heading": h["heading"],
            "source": h["source"],
            "score": round(h["score"], 3),
            "content": _sanitize_paths(engine.get_section(h["lab_name"],
                                                          h["heading"])),
        })

    # 5. Final answer from the reassembled full sections.
    context = "\n\n".join(
        f"[{n + 1}] ({s['lab_name']} :: {s['heading']})\n{s['content']}"
        for n, s in enumerate(sections)
    )
    resp = _client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0.2,
        # Room for v4-flash reasoning tokens plus the final grounded answer.
        max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Context sections:\n\n{context}\n\n"
                        f"Question: {req.question}"},
        ],
    )
    answer = _sanitize_paths(resp.choices[0].message.content)

    sources = [{"n": n + 1, **s} for n, s in enumerate(sections)]
    return {"answer": answer, "sources": sources}


@app.get("/health")
def health():
    return {"status": "ok", "chunks": len(engine.chunks), "labs": len(engine.labs)}
