"""
Build (or rebuild) the RAG index from the lab HTML files.

Run once before starting the app:
    cd rag_interview
    python3 build_index.py

Imports on this machine's NFS home can be slow on a cold cache (numpy alone
can take ~90s the first time). Be patient; it is cached afterwards.
"""

import time
import glob
import os

_t = time.time()


def log(msg):
    print(f"[{time.time() - _t:6.1f}s] {msg}", flush=True)


log("starting build")
from rag_engine import RAGEngine, INDEX_PATH, _PARENT  # noqa: E402

log("modules imported")

html_paths = sorted(glob.glob(os.path.join(_PARENT, "TC-*.html")))
if not html_paths:
    html_paths = sorted(glob.glob(os.path.join(_PARENT, "*.html")))
log(f"found {len(html_paths)} HTML files")

eng = RAGEngine()
n = eng.build(html_paths)
log(f"embedded {n} chunks across {len(eng.labs)} labs")

eng.save(INDEX_PATH)
log(f"saved index -> {INDEX_PATH}")

# Quick smoke test of retrieval.
for r in eng.search("How do I load the SRAM macro libraries?", k=3):
    log(f"  hit [{r['score']:.3f}] {r['lab_name']} :: {r['heading']}")

log("done")
