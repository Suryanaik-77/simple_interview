# Lab RAG

A small, **standalone** Retrieval-Augmented-Generation app over the VLSI lab
testcase HTML files. It is fully independent of the main interview platform —
it only reuses `../extract_html_sections.py` to parse the HTML into sections.

## How it works

1. `extract_html_sections.py` turns each `TC-*.html` lab file into clean text
   sections (chunks).
2. `rag_engine.py` embeds each chunk with OpenAI `text-embedding-3-small` and
   stores the vectors in an in-memory numpy matrix, cached to `data/index.pkl`.
   No vector database or extra services are required.
3. `app.py` (FastAPI) retrieves the most relevant chunks for a question via
   cosine similarity and asks `gpt-4.1-mini` to answer using only those chunks,
   with citations back to the source sections.

## Setup

Uses `OPENAI_API_KEY` from the parent project's `../.env`.

```bash
cd rag_interview
python3 build_index.py        # one-time: embed all lab sections -> data/index.pkl
uvicorn app:app --port 8100   # then open http://localhost:8100
```

The app also builds the index automatically on first startup if it is missing,
and rebuilds it whenever the source HTML files change.

> Note: module imports (numpy/openai) can be slow on the first run on this
> machine's NFS home; subsequent runs are cached and fast.
