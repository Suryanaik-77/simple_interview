"""
Standalone RAG engine for the lab-testcase interview.

Self-contained: reads HTML lab files via ../extract_html_sections.py, embeds
each section chunk with OpenAI embeddings, caches the vectors to a pickle, and
serves cosine-similarity retrieval. No vector-DB service required.

This module is intentionally independent of the main interview app.
"""

import os
import re
import sys
import glob
import pickle
import hashlib

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI
from langchain_text_splitters import RecursiveCharacterTextSplitter

# Import the existing extractor from the parent directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
from extract_html_sections import extract_sections  # noqa: E402

# Prefer this app's own .env (may hold a funded key); fall back to parent .env.
load_dotenv(os.path.join(_PARENT, ".env"))
load_dotenv(os.path.join(_HERE, ".env"), override=True)

EMBED_MODEL = "text-embedding-3-large"
INDEX_PATH = os.path.join(_HERE, "data", "index.pkl")

# Chunk size / overlap for the recursive character splitter.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
)

# ---------------------------------------------------------------------------
# Path sanitizing (applied ONCE at ingest, so the cached index is already clean)
#
# Internal/central mount paths must never reach a candidate. Rewrite every
# central admin/user/PDK path to the user-space working-directory convention
# (~/pd/...), matching how the labs are meant to be worded. Ordered specific ->
# general so the fallback only catches paths the explicit rules missed.
# ---------------------------------------------------------------------------
_CENTRAL_PATH_RES = (
    (re.compile(r"/proj5/semicon_labs/central_labs/labs/"), "~/pd/labs/"),
    (re.compile(r"/proj5/semicon_labs/central_labs/"), "~/pd/labs/"),
    # proj1 central design/user/PDK areas — strip the central+owner segments.
    (re.compile(r"/proj1/pd/users/testcase/[^/\s]+/"), "~/pd/"),
    (re.compile(r"/proj1/pd/pdk/"), "~/pd/pdk/"),
    (re.compile(r"/proj1/proj_pd/"), "~/pd/"),
    (re.compile(r"/proj1/dataIn/"), "~/pd/"),
    # Generic fallback: any remaining central mount -> user space.
    (re.compile(r"/proj\d+/(?:pd/)?"), "~/pd/"),
)


def sanitize_paths(text):
    """Replace central/admin/PDK mount paths with the user-facing ~/pd/ path."""
    if not text:
        return text
    for pattern, repl in _CENTRAL_PATH_RES:
        text = pattern.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# Facets parsed from the TC filename taxonomy:
#   TC-<STAGE>-<PROVIDER>-<VARIANT>-<TYPE>-<NUM>.html
# e.g. TC-LEC-SNPS-CG-GD-001 -> stage LEC, provider SNPS, variant CG, type GD.
# These let retrieval filter by tool/stage and let the clarify step offer REAL
# options (Synopsys vs Cadence) instead of an LLM guessing them.
# ---------------------------------------------------------------------------
_STAGE_LABELS = {
    "LEC": "Logic Equivalence Check",
    "PNR": "Place & Route",
    "PV": "Physical Verification",
    "STA": "Static Timing Analysis",
    "SYN": "Synthesis",
}
_PROVIDER_LABELS = {"SNPS": "Synopsys", "CDN": "Cadence", "SIE": "Siemens"}
_TYPE_LABELS = {"GD": "Guided", "CH": "Challenge", "OV": "Overview"}

_FACET_RE = re.compile(
    r"^TC-(?P<stage>[A-Z]+)-(?P<provider>[A-Z]+)-(?P<variant>.+)-"
    r"(?P<doc_type>GD|CH|OV)-(?P<num>\d+)$"
)


def parse_facets(source):
    """Parse a TC-*.html filename into structured facets (best effort)."""
    stem = source[:-5] if source.lower().endswith(".html") else source
    m = _FACET_RE.match(stem)
    if not m:
        return {}
    stage = m.group("stage")
    provider = m.group("provider")
    doc_type = m.group("doc_type")
    return {
        "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage, stage),
        "provider": provider,
        "provider_label": _PROVIDER_LABELS.get(provider, provider),
        "variant": m.group("variant"),
        "doc_type": doc_type,
        "doc_label": _TYPE_LABELS.get(doc_type, doc_type),
    }


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
            source = os.path.basename(path)
            facets = parse_facets(source)
            for sec in extract_sections(path):
                header = f"[{sec['lab_name']}]\n\n## {sec['heading']}"
                # Sanitize central paths ONCE, here, so the cached index and
                # everything downstream (retrieval, verifier, answer) is clean.
                content = sanitize_paths(sec["content"])
                parts = _splitter.split_text(content)

                # If a section splits into several chunks, prepend the header
                # to each so every chunk keeps its lab/section context.
                # If it stays a single chunk, add the header once.
                for part in parts:
                    chunks.append({
                        "heading": sec["heading"],
                        "lab_name": sec["lab_name"],
                        "content": part,
                        "chunk": f"{header}\n\n{part}",
                        "source": source,
                        "facets": facets,
                    })

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

        # Always index the learning-path / lab-catalog overview page if present.
        # It isn't a TC-* testcase, but it's the corpus source of truth for
        # "how many labs / what's the learning path / which tools per stage".
        # Its filename doesn't match the TC facet pattern, so parse_facets()
        # returns {} and it stays pure content (no facet/clarify side effects).
        overview = os.path.join(html_dir, "pd_learning_path.html")
        if os.path.exists(overview) and overview not in html_paths:
            html_paths = sorted(html_paths + [overview])

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

    def search(self, query, k=5, lab_name=None, facets=None):
        """Return the top-k chunks most similar to the query text.

        facets: optional {facet_key: value} filter (e.g. {"provider": "SNPS"})
        applied before scoring, so a clarified pick hard-restricts the results.
        """
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
            # A facet filter (e.g. picked provider=SNPS) restricts facet-bearing
            # testcases, but the tool-agnostic overview page (no facets) is
            # always relevant — keep it so a picked tool still gets the module
            # order from the learning-path doc.
            cfac = c.get("facets") or {}
            if facets and cfac and any(cfac.get(fk) != fv
                                       for fk, fv in facets.items()):
                continue
            results.append({**c, "score": float(sims[i])})
            if len(results) >= k:
                break
        return results

    def get_section(self, lab_name, heading):
        """
        Return the full section text for a (lab_name, heading) by concatenating
        all sibling sub-chunks that share that header, in original order.
        """
        parts = [c["content"] for c in self.chunks
                 if c["lab_name"] == lab_name and c["heading"] == heading]
        # De-duplicate overlapping splitter regions while preserving order.
        seen, out = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return "\n".join(out)


if __name__ == "__main__":
    eng = RAGEngine.load_or_build()
    print(f"Indexed {len(eng.chunks)} chunks across {len(eng.labs)} labs.")
    for r in eng.search("How do I load SRAM macro libraries?", k=3):
        print(f"  [{r['score']:.3f}] {r['lab_name']} :: {r['heading']}")
