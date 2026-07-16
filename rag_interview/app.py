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

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel
from openai import OpenAI

from rag_engine import RAGEngine

_HERE = os.path.dirname(os.path.abspath(__file__))

CHAT_MODEL = "gpt-4.1-mini"

app = FastAPI(title="Lab RAG")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

# Build/load the index once at startup.
engine = RAGEngine.load_or_build()
_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = (
    "You are a helpful assistant for VLSI lab testcases. Answer the user's "
    "question using ONLY the provided context sections. If the answer is not "
    "in the context, say you don't have that information. Be concise and "
    "technical. When you use a fact, cite the source section like [1], [2] "
    "matching the numbered context blocks."
)


class AskRequest(BaseModel):
    question: str
    lab_name: str | None = None
    k: int = 5


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
    hits = engine.search(req.question, k=req.k, lab_name=req.lab_name)
    if not hits:
        return {"answer": "No lab content is indexed.", "sources": []}

    context = "\n\n".join(
        f"[{i + 1}] ({h['lab_name']} :: {h['heading']})\n{h['content']}"
        for i, h in enumerate(hits)
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context sections:\n\n{context}\n\nQuestion: {req.question}",
        },
    ]
    resp = _client.chat.completions.create(
        model=CHAT_MODEL, messages=messages, temperature=0.2
    )
    answer = resp.choices[0].message.content

    sources = [
        {
            "n": i + 1,
            "lab_name": h["lab_name"],
            "heading": h["heading"],
            "source": h["source"],
            "score": round(h["score"], 3),
            "content": h["content"],
        }
        for i, h in enumerate(hits)
    ]
    return {"answer": answer, "sources": sources}


@app.get("/health")
def health():
    return {"status": "ok", "chunks": len(engine.chunks), "labs": len(engine.labs)}
