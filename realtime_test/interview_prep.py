"""
Interview preparation for the Realtime voice test app.

Reuses the SAME assets as the main platform, without importing it:
  - Persona prompts from ../prompts/{level}_{domain}.md
  - Returning-candidate history from the shared Postgres DB (or the
    file-backed ../candidate_history.json fallback), keyed by email.

Given a resume (raw text -> parsed, or already-structured fields) and an
email, build_instructions() returns the full system prompt that drives the
Realtime interviewer — mirroring main.build_interview_prompt()'s system half.
"""

import os
import json
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.abspath(os.path.join(DIR, ".."))
PROMPTS_DIR = os.path.join(PARENT, "prompts")
HISTORY_FILE = os.path.join(PARENT, "candidate_history.json")

VALID_DOMAINS = ("physical_design", "design_verification", "analog_layout")
VALID_LEVELS = ("fresh_graduate", "trained_fresher",
                "experienced_junior", "experienced_senior")

# Model for resume parsing (self-contained; unrelated to the interview qgen model).
RESUME_MODEL = os.environ.get("RESUME_PARSE_MODEL", "gpt-4.1-mini")

# ── Shared candidate history (DB first, JSON file fallback) ──────────────────

_db = None
_db_ready = False


def _get_db():
    """Import and initialize the shared database module once. Returns the module
    if a live Postgres connection is available, else None."""
    global _db, _db_ready
    if _db_ready:
        return _db
    _db_ready = True
    try:
        if PARENT not in sys.path:
            sys.path.insert(0, PARENT)
        import database  # noqa: E402
        database.init_db()
        if database.is_available():
            _db = database
    except Exception as e:  # pragma: no cover - environment dependent
        print(f"[prep] DB unavailable, using file fallback: {e}")
        _db = None
    return _db


def get_previous_sessions(email: str) -> list:
    """Return the candidate's prior session summaries (oldest -> newest)."""
    if not email:
        return []
    db = _get_db()
    if db is not None:
        try:
            return db.get_candidate_history(email) or []
        except Exception as e:
            print(f"[prep] get_candidate_history failed: {e}")
    # File fallback (matches main._load_history structure: {email: [summary,...]})
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                return json.load(f).get(email, []) or []
    except Exception as e:
        print(f"[prep] history file read failed: {e}")
    return []


# ── Prompt loading (same files main._load_prompt uses) ───────────────────────

_prompt_cache = {}


def load_prompt(level: str, domain: str) -> str:
    if level in ("fresh_graduate", "trained_fresher"):
        level = "experienced_junior"
    if domain not in VALID_DOMAINS:
        domain = "physical_design"
    key = (level, domain)
    if key in _prompt_cache:
        return _prompt_cache[key]
    path = os.path.join(PROMPTS_DIR, f"{level}_{domain}.md")
    if not os.path.exists(path):
        path = os.path.join(PROMPTS_DIR, "experienced_junior_physical_design.md")
    with open(path) as f:
        text = f.read()
    _prompt_cache[key] = text
    return text


# ── Resume file → plain text (pdf / docx / txt), before LLM parsing ──────────

def extract_resume_text(filename: str, data: bytes) -> str:
    """Extract plain text from an uploaded resume file.

    Supports PDF (pypdf, pymupdf fallback), DOCX (zip/XML, no dependency), and
    plain text. Returns "" if nothing usable could be extracted.
    """
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf"):
            return _extract_pdf(data)
        if name.endswith(".docx"):
            return _extract_docx(data)
        # .txt / .md / .rtf / unknown → best-effort decode
        return data.decode("utf-8", errors="ignore").strip()
    except Exception as e:
        print(f"[prep] resume extract failed for {filename!r}: {e}")
        return ""


def _extract_pdf(data: bytes) -> str:
    import io
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        print(f"[prep] pypdf failed: {e}")
    if len(text.strip()) < 30:          # scanned/tricky PDF → try PyMuPDF
        try:
            import fitz
            with fitz.open(stream=data, filetype="pdf") as doc:
                text = "\n".join(page.get_text() for page in doc)
        except Exception as e:
            print(f"[prep] pymupdf failed: {e}")
    return text.strip()


def _extract_docx(data: bytes) -> str:
    # DOCX is a zip; readable text lives in word/document.xml. Dependency-free:
    # turn paragraph/tab tags into whitespace, strip the rest, unescape entities.
    import io
    import re
    import zipfile
    import html as _html
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return _html.unescape(xml).strip()


# ── Resume parsing (self-contained, uses OPENAI_API_KEY already loaded) ──────

_RESUME_SYS = (
    "Extract structured VLSI candidate data from a resume. Return ONLY valid "
    "JSON with keys: candidate_name (string), years_experience (number), "
    "level (one of fresh_graduate, trained_fresher, experienced_junior, "
    "experienced_senior), domain (one of physical_design, design_verification, "
    "analog_layout), skills (array of strings), tools (array of EDA tool names), "
    "key_projects (array of objects with name and description). "
    "level: 0y/no internship=fresh_graduate; 0-1y internship only=trained_fresher; "
    "1-5y=experienced_junior; 5y+=experienced_senior. No markdown, JSON only."
)


def parse_resume(resume_text: str) -> dict:
    """Parse raw resume text into the structured dict the prompt builder expects.
    Returns {} on failure (caller can still supply fields manually)."""
    if not resume_text or len(resume_text.strip()) < 20:
        return {}
    try:
        from openai import OpenAI
        client = OpenAI(timeout=45.0, max_retries=2)
        resp = client.chat.completions.create(
            model=RESUME_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _RESUME_SYS},
                {"role": "user", "content": resume_text[:6000]},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        if data.get("candidate_name"):
            return data
    except Exception as e:
        print(f"[prep] resume parse failed: {e}")
    return {}


# ── Early-stop decision (poor-performance) ───────────────────────────────────
# The interviewer model never ends the interview itself (the persona forbids it —
# "that is handled for you"), so a dedicated judge OWNS the early-stop call, just
# like main.py's Stop Decision Agent. It stays dormant until a few questions are
# answered, then judges whether the candidate is performing so poorly that
# continuing adds no useful signal. Fails safe to CONTINUE.

STOP_MIN_Q = int(os.environ.get("RT_STOP_MIN_Q", "4"))     # dormant until this many answers
STOP_HARD_MAX = int(os.environ.get("RT_STOP_HARD_MAX", "25"))  # absolute ceiling
STOP_MODEL = os.environ.get("RT_STOP_MODEL", "gpt-4o-mini")

_STOP_RUBRIC = (
    "You are the STOP CONTROLLER for a live technical VLSI interview. Your ONLY "
    "job is to decide whether to END the interview early because the candidate is "
    "clearly and consistently underperforming, or to let it CONTINUE. You never "
    "ask questions.\n\n"
    "END only when the evidence across the answered questions is strong: the "
    "candidate has been mostly vague, thin, factually wrong, evasive, or unable to "
    "show any real depth or ownership — so continuing would add no useful signal. "
    "A few weak answers mixed with some solid ones is NOT enough — CONTINUE. "
    "Nerves, brevity, or one bad topic are NOT enough — CONTINUE. When in doubt, "
    "CONTINUE."
)


def stop_closing() -> str:
    """The fixed closing line the interviewer is made to deliver on an early stop.
    Kept graceful and non-committal (never tells the candidate they failed)."""
    return ("That's everything I wanted to cover today. Thanks for taking the time "
            "and for walking me through your work — we'll be in touch about the next "
            "steps. All the best.")


def _stop_transcript(messages, max_chars: int = 12000) -> tuple:
    """Format the client's message list into an oldest-first Q/A transcript and
    count substantive candidate answers. `messages` is [{role, text}, ...] with
    role in {'assistant','user'}."""
    lines, answered = [], 0
    for m in messages or []:
        role = (m.get("role") or "").strip()
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if role == "assistant":
            lines.append(f"Interviewer: {text}")
        elif role == "user":
            lines.append(f"Candidate: {text}")
            answered += 1
    return "\n\n".join(lines)[-max_chars:], answered


def should_stop_early(messages, level: str = "") -> dict:
    """Decide whether to END the interview early for poor performance.

    Returns {stop: bool, reason: str, answered: int}. Dormant (never stops) until
    STOP_MIN_Q answers; forces a stop at STOP_HARD_MAX. Fails safe to CONTINUE."""
    transcript, answered = _stop_transcript(messages)
    if answered < STOP_MIN_Q:
        return {"stop": False, "reason": "below_min", "answered": answered}
    if answered >= STOP_HARD_MAX:
        return {"stop": True, "reason": "hard_max", "answered": answered}
    prompt = (
        f"{_STOP_RUBRIC}\n\n"
        f"Transcript so far:\n{transcript}\n\n"
        f"--- decide now ---\n"
        f"State: {answered} questions answered (minimum {STOP_MIN_Q} met). "
        f"Candidate level: {level or 'unknown'}.\n"
        f'Return ONLY JSON: {{"decision": "end" | "continue", "reason": "<max 8 words>"}}'
    )
    try:
        from openai import OpenAI
        client = OpenAI(timeout=20.0, max_retries=1)
        resp = client.chat.completions.create(
            model=STOP_MODEL,
            temperature=0.0,
            max_tokens=40,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        decision = str(data.get("decision", "continue")).strip().lower()
        reason = str(data.get("reason", ""))[:60] or decision
        return {"stop": decision == "end", "reason": reason, "answered": answered}
    except Exception as e:
        print(f"[prep] stop decision failed ({e}) - continuing")
        return {"stop": False, "reason": "error", "answered": answered}


# ── Instruction builder (mirrors main.build_interview_prompt system half) ────

_SKIP_PHRASES = {
    "good morning", "good afternoon", "good evening", "tell me about yourself",
    "welcome back", "thanks for coming", "don't go personal", "let's focus",
    "please answer in english", "take your time", "that covers what i needed",
    "thank you for your time", "i'll decide what to ask", "let's continue",
    "let's move on",
}


def _returning_block(email: str) -> tuple:
    """Return (block_text, meta) for a returning candidate's last 2 sessions."""
    prev = get_previous_sessions(email)
    if not prev:
        return "", {"previous_sessions": 0, "prev_questions": [], "prev_projects": []}

    recent = prev[-2:]
    prev_questions, prev_projects = [], set()
    for ps in recent:
        for q in ps.get("questions_asked", []):
            ql = (q or "").strip().lower()
            if any(ql.startswith(p) for p in _SKIP_PHRASES) or len(ql) < 15:
                continue
            prev_questions.append(q)
        for p in ps.get("projects", []):
            prev_projects.add(p.get("name", str(p)) if isinstance(p, dict) else str(p))

    projects_note = ""
    if prev_projects:
        projects_note = (
            f"\nProjects discussed before: {', '.join(sorted(prev_projects))}\n"
            "Ask about DIFFERENT aspects of these projects, or explore projects "
            "not yet discussed.")

    block = f"""
RETURNING CANDIDATE: This candidate has interviewed {len(prev)} time(s) before.
These questions were already asked in previous sessions:
{chr(10).join(f'- {q}' for q in prev_questions)}{projects_note}
This is a completely NEW interview. Ask fresh questions from different angles on the same topics.
Test whether the candidate has genuinely improved or just memorized answers from before."""

    return block, {
        "previous_sessions": len(prev),
        "prev_questions": prev_questions,
        "prev_projects": sorted(prev_projects),
    }


def _format_projects(raw_projects) -> str:
    if raw_projects and isinstance(raw_projects[0], dict):
        lines = []
        for p in raw_projects:
            name, desc = p.get("name", ""), p.get("description", "")
            lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
        return "\n".join(lines)
    return ", ".join(str(p) for p in raw_projects) if raw_projects else "not specified"


def build_instructions(resume: dict, email: str = "") -> dict:
    """Build the full Realtime interviewer system prompt.

    resume: structured dict (candidate_name, level, domain, years_experience,
            tools, skills, key_projects, resume_text). email: for history lookup.
    Returns {instructions, meta}.
    """
    resume = resume or {}
    name = resume.get("candidate_name") or "Candidate"
    level = resume.get("level") or "experienced_junior"
    domain = resume.get("domain") or "physical_design"
    years = resume.get("years_experience", 0)
    tools = ", ".join(str(t) for t in resume.get("tools", [])) or "not specified"
    skills = ", ".join(str(s) for s in resume.get("skills", [])) or "not specified"
    projects_str = _format_projects(resume.get("key_projects", []))

    base = load_prompt(level, domain)

    if "\n" in projects_str:
        info = (f"\nCANDIDATE: {name} | {level.replace('_', ' ')} | {years} years | "
                f"Tools: {tools} | Skills: {skills}\nProjects:\n{projects_str}")
    else:
        info = (f"\nCANDIDATE: {name} | {level.replace('_', ' ')} | {years} years | "
                f"Tools: {tools} | Projects: {projects_str} | Skills: {skills}")

    resume_text = resume.get("resume_text", "")
    if resume_text:
        info += f"\n\nFULL RESUME:\n{resume_text[:2000]}"

    ret_block, meta = _returning_block(email)
    instructions = base + info + ret_block

    meta.update({"name": name, "level": level, "domain": domain})
    return {"instructions": instructions, "meta": meta}
