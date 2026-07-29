"""
Analog Layout RAG engine for first 5 topics.

Separate from PD and DV RAGs:
- Uses analog_corpus/ directory (HTML files from Analog Layout topics)
- Separate vector database (data/analog_index.pkl)
- No TC-* facet parsing (Analog topics don't use that taxonomy)
- Focused on analog layout fundamentals

Topics covered:
1. Linux Command Line Tools
2. Basic Electronics
3. Wafer Process
4. Transistors
5. Power Dissipation
"""

import os
import sys
import glob
import pickle
import hashlib

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Import the HTML section extractor from parent directory
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
from extract_html_sections import extract_sections  # noqa: E402

# Load environment
load_dotenv(os.path.join(_PARENT, ".env"))
load_dotenv(os.path.join(_HERE, ".env"), override=True)

EMBED_MODEL = "text-embedding-3-large"
ANALOG_INDEX_PATH = os.path.join(_HERE, "data", "analog_index.pkl")
ANALOG_CORPUS_DIR = os.path.join(_HERE, "analog_corpus")

# Chunk size / overlap for Analog content
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
)

_client = None


def _openai():
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
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


class AnalogRAGEngine:
    """In-memory vector index over Analog Layout topic sections."""

    def __init__(self):
        self.chunks = []          # list of dicts: heading, topic_name, content, chunk, source
        self.vectors = None       # (n, dim) normalized float32
        self._fingerprint = None  # hash of source files

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
        """Extract chunks from Analog HTML files and embed them."""
        chunks = []
        for path in sorted(html_paths):
            source = os.path.basename(path)
            # Extract topic name from filename: Topic2_Linux_tools_...html
            topic_parts = source.split("_", 1)
            if len(topic_parts) >= 2:
                topic_name = topic_parts[0] + "_" + topic_parts[1].split("_")[0]
            else:
                topic_name = source.replace(".html", "")

            for sec in extract_sections(path):
                header = f"[{topic_name}]\n\n## {sec['heading']}"
                content = sec["content"]
                parts = _splitter.split_text(content)

                for part in parts:
                    chunks.append({
                        "heading": sec["heading"],
                        "topic_name": topic_name,
                        "content": part,
                        "chunk": f"{header}\n\n{part}",
                        "source": source,
                    })

        if not chunks:
            raise RuntimeError("No chunks extracted from Analog HTML files.")

        texts = [c["chunk"] for c in chunks]
        vecs = []
        for i in range(0, len(texts), batch_size):
            vecs.append(_embed_batch(texts[i:i + batch_size]))
        vectors = _normalize(np.vstack(vecs))

        self.chunks = chunks
        self.vectors = vectors
        self._fingerprint = self._fingerprint_files(html_paths)
        return len(chunks)

    def save(self, path=ANALOG_INDEX_PATH):
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

    def load(self, path=ANALOG_INDEX_PATH):
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.chunks = data["chunks"]
        self.vectors = data["vectors"]
        self._fingerprint = data.get("fingerprint")
        return len(self.chunks)

    @classmethod
    def load_or_build(cls, corpus_dir=ANALOG_CORPUS_DIR, index_path=ANALOG_INDEX_PATH):
        """Load cached Analog index, rebuilding if source files changed."""
        html_paths = sorted(glob.glob(os.path.join(corpus_dir, "*.html")))

        if not html_paths:
            raise RuntimeError(f"No HTML files found in {corpus_dir}")

        eng = cls()
        current_fp = cls._fingerprint_files(html_paths)

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
    def topics(self):
        """Unique topic names in the index."""
        seen = []
        for c in self.chunks:
            if c["topic_name"] not in seen:
                seen.append(c["topic_name"])
        return seen

    def search(self, query, k=5, topic_name=None):
        """Return the top-k chunks most similar to the query text."""
        if self.vectors is None or not len(self.chunks):
            return []
        qvec = _normalize(_embed_batch([query]))[0]
        sims = self.vectors @ qvec  # cosine (vectors are normalized)

        idx = np.argsort(-sims)
        results = []
        for i in idx:
            c = self.chunks[i]
            if topic_name and c["topic_name"] != topic_name:
                continue
            results.append({**c, "score": float(sims[i])})
            if len(results) >= k:
                break
        return results

    def get_section(self, topic_name, heading):
        """
        Return the full section text for a (topic_name, heading) by concatenating
        all sibling sub-chunks that share that header.
        """
        parts = [c["content"] for c in self.chunks
                 if c["topic_name"] == topic_name and c["heading"] == heading]
        # De-duplicate overlapping splitter regions
        seen, out = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return "\n".join(out)


if __name__ == "__main__":
    eng = AnalogRAGEngine.load_or_build()
    print(f"Indexed {len(eng.chunks)} chunks across {len(eng.topics)} topics.")
    print(f"\nTopics: {', '.join(eng.topics)}")
    print("\n--- Sample search: 'What is MOSFET?' ---")
    for r in eng.search("What is MOSFET?", k=3):
        print(f"  [{r['score']:.3f}] {r['topic_name']} :: {r['heading']}")
