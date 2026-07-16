"""
Standalone RAG engine for the lab-testcase interview.

Self-contained: reads HTML lab files via ../extract_html_sections.py, embeds
each section chunk with OpenAI embeddings, caches the vectors to a pickle, and
serves cosine-similarity retrieval. No vector-DB service required.

This module is intentionally independent of the main interview app.
"""

import os
import sys
import glob
import pickle
import hashlib

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

# Import the existing extractor from the parent directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
from extract_html_sections import extract_sections  # noqa: E402

load_dotenv(os.path.join(_PARENT, ".env"))

EMBED_MODEL = "text-embedding-3-small"
INDEX_PATH = os.path.join(_HERE, "data", "index.pkl")

_client = None


def _openai():
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set (checked ../.env)")
        _client = OpenAI(api_key=key, timeout=60.0, max_retries=2)
    return _client


def _embed_batch(texts):
    """Embed a list of texts, returning an (n, dim) float32 array."""
    resp = _openai().embeddings.create(model=EMBED_MODEL, input=texts)
    return np.array([d.embedding for d in resp.data], dtype=np.float32)


def _normalize(mat):
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class RAGEngine:
    """In-memory vector index over lab-testcase section chunks."""

    def __init__(self):
        self.chunks = []          # list of dicts: heading, lab_name, content, chunk, source
        self.vectors = None       # (n, dim) normalized float32
        self._fingerprint = None  # hash of source files, to detect staleness

    # ---- building / loading -------------------------------------------------

    @staticmethod
    def _fingerprint_files(html_paths):
        h = hashlib.sha256()
        for p in sorted(html_paths):
            st = os.stat(p)
            h.update(p.encode())
            h.update(str(st.st_mtime_ns).encode())
            h.update(str(st.st_size).encode())
        return h.hexdigest()

    def build(self, html_paths, batch_size=64):
        """Extract chunks from the given HTML files and embed them."""
        chunks = []
        for path in sorted(html_paths):
            for sec in extract_sections(path):
                sec = dict(sec)
                sec["source"] = os.path.basename(path)
                chunks.append(sec)

        if not chunks:
            raise RuntimeError("No chunks extracted from the provided HTML files.")

        texts = [c["chunk"] for c in chunks]
        vecs = []
        for i in range(0, len(texts), batch_size):
            vecs.append(_embed_batch(texts[i:i + batch_size]))
        vectors = _normalize(np.vstack(vecs))

        self.chunks = chunks
        self.vectors = vectors
        self._fingerprint = self._fingerprint_files(html_paths)
        return len(chunks)

    def save(self, path=INDEX_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "chunks": self.chunks,
                    "vectors": self.vectors,
                    "fingerprint": self._fingerprint,
                    "embed_model": EMBED_MODEL,
                },
                f,
            )

    def load(self, path=INDEX_PATH):
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.chunks = data["chunks"]
        self.vectors = data["vectors"]
        self._fingerprint = data.get("fingerprint")
        return len(self.chunks)

    @classmethod
    def load_or_build(cls, html_dir=None, index_path=INDEX_PATH):
        """Load a cached index, rebuilding if source files changed."""
        html_dir = html_dir or _PARENT
        html_paths = sorted(glob.glob(os.path.join(html_dir, "TC-*.html")))
        if not html_paths:
            html_paths = sorted(glob.glob(os.path.join(html_dir, "*.html")))

        eng = cls()
        current_fp = cls._fingerprint_files(html_paths) if html_paths else None

        if os.path.exists(index_path):
            eng.load(index_path)
            if eng._fingerprint == current_fp:
                return eng  # cache is fresh

        # Stale or missing -> rebuild
        eng.build(html_paths)
        eng.save(index_path)
        return eng

    # ---- retrieval ----------------------------------------------------------

    @property
    def labs(self):
        """Unique lab/module names in the index."""
        seen = []
        for c in self.chunks:
            if c["lab_name"] not in seen:
                seen.append(c["lab_name"])
        return seen

    def search(self, query, k=5, lab_name=None):
        """Return the top-k chunks most similar to the query text."""
        if self.vectors is None or not len(self.chunks):
            return []
        qvec = _normalize(_embed_batch([query]))[0]
        sims = self.vectors @ qvec  # cosine (vectors are normalized)

        idx = np.argsort(-sims)
        results = []
        for i in idx:
            c = self.chunks[i]
            if lab_name and c["lab_name"] != lab_name:
                continue
            results.append({**c, "score": float(sims[i])})
            if len(results) >= k:
                break
        return results


if __name__ == "__main__":
    eng = RAGEngine.load_or_build()
    print(f"Indexed {len(eng.chunks)} chunks across {len(eng.labs)} labs.")
    for r in eng.search("How do I load SRAM macro libraries?", k=3):
        print(f"  [{r['score']:.3f}] {r['lab_name']} :: {r['heading']}")
