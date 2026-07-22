"""
Generic HTML section extractor for course/lab modules.

Extracts structured sections from any HTML file and returns them as
a list of text chunks, each prefixed with the module/lab name.

Works with any HTML structure:
  - Splits by <div class="section"> if present
  - Falls back to splitting by <h2> headings
  - Falls back to splitting by any heading tags

Usage:
    python extract_html_sections.py <html_file> [--json]

    from extract_html_sections import extract_sections
    sections = extract_sections("module.html")
"""

import re
import sys
import json
import os
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    """Extract visible text from an HTML fragment, preserving structure."""

    BLOCK_TAGS = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "br", "hr", "pre", "blockquote", "section",
    }
    SKIP_TAGS = {"script", "style", "head", "meta", "link"}

    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "li":
            self._parts.append("- ")
        if tag == "code":
            self._parts.append("`")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "code":
            self._parts.append("`")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._parts.append(data)

    def get_text(self):
        raw = "".join(self._parts)
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
        return "\n".join(lines)


def _html_to_text(html_fragment):
    """Convert an HTML fragment to clean text."""
    ext = _TextExtractor()
    ext.feed(html_fragment)
    return ext.get_text()


def _find_module_name(html):
    """Extract the module/lab name from the HTML."""
    # Try <h1>
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
    if m:
        # Collapse internal whitespace: an <h1> with <br/> (e.g. "Recommended
        # <br/>Learning Path") must not carry a newline into the lab name.
        return re.sub(r"\s+", " ", _html_to_text(m.group(1))).strip()
    # Try <title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if m:
        name = _html_to_text(m.group(1)).strip()
        # Remove generic suffixes
        for suffix in [" - VLSI", " | ", " — "]:
            if suffix in name:
                name = name.split(suffix)[0].strip()
        return name
    # Try first <h2>
    m = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return _html_to_text(m.group(1)).strip()
    return "Unknown Module"


def _find_section_heading(section_html):
    """Extract the heading text from a section."""
    for tag in ["h2", "h3", "h1"]:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", section_html, re.DOTALL | re.IGNORECASE)
        if m:
            text = re.sub(r"\s+", " ", _html_to_text(m.group(1))).strip()
            # Remove emoji characters (common in course HTML)
            text = re.sub(r'[\U0001f300-\U0001f9ff☀-➿✀-➿]', '', text).strip()
            # Remove leading/trailing special chars
            text = re.sub(r'^[?\s]+|[?\s]+$', '', text).strip()
            if text:
                return text
    return None


def _split_by_section_divs(html):
    """Split HTML into top-level content blocks by their wrapper div class.

    Recognizes the common lab-page wrappers (section / content-box) plus the
    learning-path page's cards (stage / rationale). Splitting on the card
    boundary keeps each block self-contained — e.g. a stage card's own node
    number and "Stage N" label stay inside that card instead of bleeding into
    the previous section (which an <h2> split does, since those markers sit
    before the next card's <h2>).
    """
    pattern = r'(<div\s+class\s*=\s*["\'](?:section|content-box|stage|rationale)["\'][^>]*>)'
    parts = re.split(pattern, html, flags=re.IGNORECASE)

    sections = []
    i = 0
    while i < len(parts):
        if re.match(pattern, parts[i], re.IGNORECASE):
            if i + 1 < len(parts):
                sections.append(parts[i] + parts[i + 1])
                i += 2
            else:
                sections.append(parts[i])
                i += 1
        else:
            # Content before the first section (header area)
            if parts[i].strip():
                if re.search(r'<div\s+class\s*=\s*["\']header', parts[i], re.IGNORECASE):
                    sections.insert(0, parts[i])
                elif re.search(r'<header\b', parts[i], re.IGNORECASE):
                    sections.insert(0, parts[i])
                elif re.search(r'<h[12][^>]*>', parts[i], re.IGNORECASE):
                    sections.insert(0, parts[i])
            i += 1
    return sections


def _split_by_headings(html, tag="h2"):
    """Split HTML at each <h2> (or specified tag) boundary."""
    # Find all heading positions
    heading_pattern = re.compile(rf'<{tag}[^>]*>.*?</{tag}>', re.DOTALL | re.IGNORECASE)
    matches = list(heading_pattern.finditer(html))
    if not matches:
        return []

    sections = []
    # Content before the first heading (header area)
    pre = html[:matches[0].start()].strip()
    if pre and re.search(r'<h[1-6][^>]*>', pre, re.IGNORECASE):
        sections.append(pre)

    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(html)
        sections.append(html[start:end])

    return sections


_NAV_LINE_RE = re.compile(
    r'^\s*(?:[←→‹›⟵⟶]|Back to\b|Next\b|Mark Complete\b)',
    re.IGNORECASE,
)


def _strip_nav_lines(content):
    """Remove standalone navigation-button lines from section text."""
    kept = [ln for ln in content.split("\n") if not _NAV_LINE_RE.match(ln)]
    return "\n".join(kept).strip()


def extract_sections(html_path, lab_name=None):
    """
    Extract sections from an HTML file.

    Args:
        html_path: Path to the HTML file
        lab_name:  Override module/lab name (auto-detected if None)

    Returns:
        List of dicts: [{"heading": str, "content": str, "chunk": str}, ...]
        where "chunk" is the final text with lab name prepended.
    """
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()

    if not lab_name:
        lab_name = _find_module_name(html)

    # Strategy 1: Split by <div class="section">
    raw_sections = _split_by_section_divs(html)

    # Strategy 2: Fall back to <h2> splitting
    if len(raw_sections) < 2:
        raw_sections = _split_by_headings(html, "h2")

    # Strategy 3: Fall back to <h3> splitting
    if len(raw_sections) < 2:
        raw_sections = _split_by_headings(html, "h3")

    # Strategy 4: Whole document as one chunk
    if len(raw_sections) < 1:
        raw_sections = [html]

    results = []
    for sec_html in raw_sections:
        heading = _find_section_heading(sec_html)
        content = _html_to_text(sec_html)
        if not content.strip():
            continue

        # Drop the heading line if it's duplicated as the first line of content
        if heading:
            lines = content.split("\n")
            if lines and lines[0].strip() == heading.strip():
                content = "\n".join(lines[1:]).strip()

        # Strip nav-button lines (← Back to..., → Next..., Mark Complete ✓)
        content = _strip_nav_lines(content)

        # Skip chunks that are empty after cleanup (pure nav/footer blocks)
        if not content.strip():
            continue

        heading = heading or "Introduction"

        # Build final chunk with lab name prepended
        chunk = f"[{lab_name}]\n\n## {heading}\n\n{content}"

        results.append({
            "heading": heading,
            "lab_name": lab_name,
            "content": content,
            "chunk": chunk,
        })

    return results


def extract_all(html_dir, lab_name=None, extension=".html"):
    """
    Extract sections from all HTML files in a directory.

    Returns:
        List of all chunks across all files.
    """
    all_chunks = []
    for root, _dirs, files in os.walk(html_dir):
        for fname in sorted(files):
            if not fname.endswith(extension):
                continue
            path = os.path.join(root, fname)
            sections = extract_sections(path, lab_name=lab_name)
            all_chunks.extend(sections)
    return all_chunks


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_html_sections.py <html_file_or_dir> [--json] [--lab 'Lab Name']")
        sys.exit(1)

    target = sys.argv[1]
    as_json = "--json" in sys.argv
    lab_override = None
    if "--lab" in sys.argv:
        idx = sys.argv.index("--lab")
        if idx + 1 < len(sys.argv):
            lab_override = sys.argv[idx + 1]

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if os.path.isdir(target):
        sections = extract_all(target, lab_name=lab_override)
    else:
        sections = extract_sections(target, lab_name=lab_override)

    if as_json:
        print(json.dumps(sections, indent=2, ensure_ascii=False))
    else:
        for i, sec in enumerate(sections):
            print(f"\n{'='*60}")
            print(f"CHUNK {i+1}: {sec['heading']}")
            print(f"{'='*60}")
            print(sec["chunk"])
            print()

        print(f"\n--- Total: {len(sections)} chunks extracted ---")
