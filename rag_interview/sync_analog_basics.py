#!/usr/bin/env python3
"""
Sync Analog Layout BASICS topics (first 5) from Zoho WorkDrive.

Topics:
1. Topic2_Linux_tools
2. Topic3_Basic Electronics
3. Topic4_Wafer Process
4. Topic6_Transistors
5. Topic9_Power Dissipation

Downloads HTML files into rag_interview/analog_corpus/
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

# Analog Layout folder ID
ANALOG_FOLDER_ID = "c2vma950f124ed8014697b4360827f8d9d0c9"

# First 5 topics (folder IDs from listing)
TOPIC_FOLDERS = {
    "Topic2_Linux_tools": "c2vmaaac82ab21c384aa986b628227aa99925",
    "Topic3_Basic_Electronics": "c2vmab9abd31590194a428b6d3d654ccf54ca",
    "Topic4_Wafer_Process": "c2vmab98cd919fc8f4c559f0ff6a048e37835",
    "Topic6_Transistors": "c2vmaa963f90b149c42d4b030b78ad72c1a2b",
    "Topic9_Power_Dissipation": "c2vmafd2cfb587d8d4b9a8c18967b6cb2c0be",
}

# Output directory for Analog corpus
ANALOG_CORPUS_DIR = os.path.join(_HERE, "analog_corpus")


def get_access_token():
    """Get Zoho access token."""
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
    """List all items in a folder."""
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
    """Clean filename for filesystem."""
    if name.endswith(".html.html"):
        return name[:-5]
    return name


def main():
    pull = "--pull" in sys.argv

    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        raise SystemExit("Missing ZOHO_* config in rag_interview/.env")

    # Create Analog corpus directory if pulling
    if pull and not os.path.exists(ANALOG_CORPUS_DIR):
        os.makedirs(ANALOG_CORPUS_DIR)
        print(f"Created Analog corpus directory: {ANALOG_CORPUS_DIR}\n")

    token = get_access_token()

    all_html_files = []

    # Process each topic folder
    for topic_name, folder_id in TOPIC_FOLDERS.items():
        print(f"\n{'='*60}")
        print(f"Processing {topic_name}...")
        print('='*60)

        items = list_folder(token, folder_id)

        # Filter only HTML files
        html_files = []
        for item in items:
            attrs = item.get("attributes", {})
            extn = (attrs.get("extn") or "").lower()
            if extn == "html":
                name = attrs.get("name", "unknown")
                item_id = item.get("id")
                size = attrs.get("storage_info", {}).get("size", "0")
                html_files.append({
                    "id": item_id,
                    "name": name,
                    "size": size,
                    "topic": topic_name
                })

        print(f"Found {len(html_files)} HTML files")
        all_html_files.extend(html_files)

    # Sort by topic and name
    all_html_files.sort(key=lambda x: (x["topic"], x["name"]))

    print(f"\n\n{'='*60}")
    print(f"TOTAL: {len(all_html_files)} HTML files across 5 topics")
    print('='*60)

    new, updated, unchanged = 0, 0, 0
    for f in all_html_files:
        clean_name = clean_filename(f["name"] + ".html")
        # Prefix with topic for organization
        dest_name = f"{f['topic']}_{clean_name}"
        dest = os.path.join(ANALOG_CORPUS_DIR, dest_name)
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

        print(f"  [{tag:8}] {dest_name} ({f['size']})")

    print()
    if pull:
        print(f"Downloaded to {ANALOG_CORPUS_DIR}:")
        print(f"  {new} new, {updated} updated, {unchanged} unchanged")
        print("\nRun analog RAG to build vector database from these files.")
    else:
        print(f"Dry run - nothing written. Re-run with --pull to download.")


if __name__ == "__main__":
    main()
