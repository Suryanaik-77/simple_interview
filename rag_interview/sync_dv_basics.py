#!/usr/bin/env python3
"""
Sync DV BASICS HTML files from Zoho WorkDrive for the DV RAG.

Downloads HTML files from DDF_DV/BASICS folder into rag_interview/dv_corpus/
for the Design Verification RAG vector database.

Usage:
    python sync_dv_basics.py          # dry run: list files
    python sync_dv_basics.py --pull   # download HTML files
"""
import os
import sys
import json
import urllib.parse
import urllib.request
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))

DC = os.getenv("ZOHO_DC", "in")
CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN")

ACCOUNTS = f"https://accounts.zoho.{DC}"
API = f"https://www.zohoapis.{DC}/workdrive/api/v1"
DOWNLOAD = f"https://download.zoho.{DC}/v1/workdrive/download"

# DDF_DV/BASICS folder ID
BASICS_FOLDER_ID = "c2vma55c780b9a43a42f8824f584dc9640810"

# Output directory for DV corpus (separate from PD)
DV_CORPUS_DIR = os.path.join(_HERE, "dv_corpus")


def get_access_token():
    """Get Zoho access token using refresh token."""
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }).encode()
    req = urllib.request.Request(f"{ACCOUNTS}/oauth/v2/token", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        tok = json.loads(resp.read())
    if "access_token" not in tok:
        raise SystemExit(f"Token refresh failed: {tok}")
    return tok["access_token"]


def list_folder(token, folder_id):
    """List all items in a folder (with pagination)."""
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Accept": "application/vnd.api+json"
    }
    items = []
    offset, limit = 0, 50
    while True:
        q = urllib.parse.urlencode({"page[limit]": limit, "page[offset]": offset})
        req = urllib.request.Request(f"{API}/files/{folder_id}/files?{q}", headers=headers)
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        rows = data.get("data", [])
        items.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
    return items


def download_file(token, file_id):
    """Download file content by ID."""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    req = urllib.request.Request(f"{DOWNLOAD}/{file_id}", headers=headers)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def clean_filename(name):
    """Remove .html.html double extension if present."""
    if name.endswith(".html.html"):
        return name[:-5]  # Remove one .html
    return name


def main():
    pull = "--pull" in sys.argv

    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        raise SystemExit("Missing ZOHO_* config in rag_interview/.env")

    # Create DV corpus directory if pulling
    if pull and not os.path.exists(DV_CORPUS_DIR):
        os.makedirs(DV_CORPUS_DIR)
        print(f"Created DV corpus directory: {DV_CORPUS_DIR}\n")

    token = get_access_token()
    items = list_folder(token, BASICS_FOLDER_ID)

    # Filter only HTML files
    html_files = []
    for item in items:
        attrs = item.get("attributes", {})
        extn = (attrs.get("extn") or "").lower()
        if extn == "html":
            name = attrs.get("name", "unknown")
            item_id = item.get("id")
            size = attrs.get("storage_info", {}).get("size", "0")
            html_files.append({"id": item_id, "name": name, "size": size})

    # Sort by name
    html_files.sort(key=lambda x: x["name"])

    print(f"Found {len(html_files)} HTML file(s) in DDF_DV/BASICS:\n")

    new, updated, unchanged = 0, 0, 0
    for f in html_files:
        clean_name = clean_filename(f["name"] + ".html")
        dest = os.path.join(DV_CORPUS_DIR, clean_name)
        exists = os.path.exists(dest)

        if pull:
            data = download_file(token, f["id"])
            old = open(dest, "rb").read() if exists else None
            if old is None:
                open(dest, "wb").write(data)
                new += 1
                tag = "NEW"
            elif old != data:
                open(dest, "wb").write(data)
                updated += 1
                tag = "UPDATED"
            else:
                unchanged += 1
                tag = "same"
        else:
            tag = "exists" if exists else "NEW"

        print(f"  [{tag:8}] {clean_name} ({f['size']})")

    print()
    if pull:
        print(f"Downloaded to {DV_CORPUS_DIR}: {new} new, {updated} updated, {unchanged} unchanged.")
        if new or updated:
            print("Run the DV RAG to build the vector database from these files.")
    else:
        print(f"Dry run - nothing written. Re-run with --pull to download.")


if __name__ == "__main__":
    main()
