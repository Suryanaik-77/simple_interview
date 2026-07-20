#!/usr/bin/env python3
"""
Sync lab TC-*.html files from Zoho WorkDrive into the RAG corpus.

Walks the configured WorkDrive folder recursively, downloads every TC-*.html
into the repo root (the RAG's html_dir = _PARENT). The RAG rebuilds its index
automatically on the next start/query because RAGEngine.load_or_build()
fingerprints the source files and rebuilds when they change.

Usage:
    python zoho_sync.py                 # dry run: list WorkDrive html vs local
    python zoho_sync.py --pull          # download all TC-*.html into the corpus
    python zoho_sync.py --pull --all-html   # include non-TC .html files too

Config (from rag_interview/.env):
    ZOHO_DC, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
    ZOHO_LAB_FOLDER_ID
"""
import os
import sys
import json
import urllib.parse
import urllib.request

from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.abspath(os.path.join(_HERE, ".."))
load_dotenv(os.path.join(_HERE, ".env"))

DC = os.getenv("ZOHO_DC", "in")
CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")
FOLDER_ID = os.getenv("ZOHO_LAB_FOLDER_ID")

ACCOUNTS = f"https://accounts.zoho.{DC}"
API = f"https://www.zohoapis.{DC}/workdrive/api/v1"
DOWNLOAD = f"https://download.zoho.{DC}/v1/workdrive/download"

TARGET_DIR = _PARENT           # where the RAG globs TC-*.html
# Noise / non-lab content. LEC-CDN-R2N is skipped: it's a mislabeled duplicate
# of LEC-SNPS-R2N (Synopsys Formality content, 0 Cadence/Conformal refs). The
# correct LEC-SNPS-R2N (AES + CVA6 RTL-to-Gate LEC) IS synced.
SKIP_FOLDERS = {"Deleted", "Lab exams", "Videos", "LEC-CDN-R2N"}
# The LEC-SNPS-R2N *folder* also holds stray files mis-named TC-LEC-CDN-R2N-*
# (same Synopsys content, wrong CDN label). Skip those files by name too, so a
# folder skip isn't enough to miss them. Matched case-insensitively by prefix.
SKIP_FILE_PREFIXES = ("TC-LEC-CDN-R2N",)


def _skip_file(name):
    """True if this html file name is excluded from the corpus."""
    n = (name or "").lower()
    return any(n.startswith(p.lower()) for p in SKIP_FILE_PREFIXES)


def corpus_name(name):
    """Map a WorkDrive html filename to its RAG-corpus name.

    Testcase pages already start with TC- and are kept as-is. The per-folder
    parent/overview page (e.g. LEC-SNPS-CG.html) is renamed to a TC- form
    (TC-LEC-SNPS-CG-OV-000.html) so the RAG's TC-*.html glob picks it up.
    """
    if name.startswith("TC-"):
        return name
    stem = name[:-5] if name.lower().endswith(".html") else name
    return f"TC-{stem}-OV-000.html"


def _req(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.status, resp.read()


def get_access_token():
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }).encode()
    _, raw = _req(f"{ACCOUNTS}/oauth/v2/token", data=body, method="POST")
    tok = json.loads(raw)
    if "access_token" not in tok:
        raise SystemExit(f"Token refresh failed: {tok}")
    return tok["access_token"]


def list_folder(token, folder_id):
    """Yield item dicts for a folder, following pagination."""
    headers = {"Authorization": f"Zoho-oauthtoken {token}",
               "Accept": "application/vnd.api+json"}
    offset, limit = 0, 50
    while True:
        q = urllib.parse.urlencode({"page[limit]": limit, "page[offset]": offset})
        _, raw = _req(f"{API}/files/{folder_id}/files?{q}", headers=headers)
        rows = json.loads(raw).get("data", [])
        for r in rows:
            yield r
        if len(rows) < limit:
            break
        offset += limit


def walk_html(token, folder_id, prefix=""):
    """Recursively collect .html files as {id, name, path}."""
    out = []
    for r in list_folder(token, folder_id):
        a = r.get("attributes", {})
        name = a.get("name", "")
        rid = r.get("id")
        if a.get("is_folder"):
            if name in SKIP_FOLDERS or name.startswith("deleted-"):
                continue
            out += walk_html(token, rid, prefix + name + "/")
        elif (a.get("extn") or "").lower() == "html":
            if _skip_file(name):
                continue
            out.append({"id": rid, "name": name, "path": prefix + name})
    return out


def download(token, file_id):
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    _, raw = _req(f"{DOWNLOAD}/{file_id}", headers=headers)
    return raw


def main():
    pull = "--pull" in sys.argv
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, FOLDER_ID]):
        raise SystemExit("Missing ZOHO_* config in rag_interview/.env")

    token = get_access_token()
    files = walk_html(token, FOLDER_ID)
    # By default pull BOTH the TC- testcase pages and the per-folder overview
    # page (renamed to TC-...-OV-000). --tc-only keeps just the testcases.
    if "--tc-only" in sys.argv:
        files = [f for f in files if f["name"].startswith("TC-")]
    files.sort(key=lambda f: f["path"])

    print(f"Found {len(files)} html file(s) in WorkDrive:\n")
    new, updated, unchanged, skipped = 0, 0, 0, 0
    seen_names = {}          # corpus-name -> source path that claimed it
    for f in files:
        cname = corpus_name(f["name"])
        # The RAG corpus is flat, so two files mapping to the same corpus name
        # would collide. Keep the first, refuse to clobber with the rest.
        if cname in seen_names:
            print(f"  [COLLIDE ] {f['path']}  (skipped; {cname} already taken by "
                  f"{seen_names[cname]})")
            skipped += 1
            continue
        seen_names[cname] = f["path"]

        rename = "" if cname == f["name"] else f"  ->  {cname}"
        dest = os.path.join(TARGET_DIR, cname)
        exists = os.path.exists(dest)
        if pull:
            data = download(token, f["id"])
            old = open(dest, "rb").read() if exists else None
            if old is None:
                open(dest, "wb").write(data); new += 1; tag = "NEW"
            elif old != data:
                open(dest, "wb").write(data); updated += 1; tag = "UPDATED"
            else:
                unchanged += 1; tag = "same"
        else:
            tag = "exists" if exists else "NEW"
        print(f"  [{tag:8}] {f['path']}{rename}")

    print()
    if pull:
        print(f"Pulled into {TARGET_DIR}: {new} new, {updated} updated, "
              f"{unchanged} unchanged, {skipped} skipped (name collision).")
        if new or updated:
            print("The RAG will rebuild its index on next start/query "
                  "(source fingerprint changed).")
    else:
        print(f"Dry run - nothing written ({skipped} name collision(s) would be "
              f"skipped). Re-run with --pull to download.")


if __name__ == "__main__":
    main()
