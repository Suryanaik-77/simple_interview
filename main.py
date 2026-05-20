"""
Simple Interview Agent — Voice AI Mock Interview
Everything from the monolith EXCEPT: strategy engine, repetition guard,
question pipeline, evaluation pipeline, evaluation validator.

Question generation: conversation history + resume → LLM → next question.
That's it. No complex routing.
"""
import os, time, json, re, secrets, tempfile, base64, threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
import requests as http_requests

# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Simple Interview Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Config ───────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
INWORLD_API_KEY = os.getenv("INWORLD_API_KEY", "")
INWORLD_VOICE_ID = os.getenv("INWORLD_VOICE_ID", "Sarah")
INWORLD_MODEL_ID = os.getenv("INWORLD_MODEL_ID", "inworld-tts-1.5-mini")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

xai_client = None
if XAI_API_KEY:
    xai_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

cerebras_client = None
if CEREBRAS_API_KEY:
    cerebras_client = OpenAI(api_key=CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1")
    print("Cerebras LLM ready.")

bedrock_client = None
try:
    import boto3
    bedrock_client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
except: pass

# ── Runtime Config ───────────────────────────────────────────────────────
RUNTIME_CONFIG = {
    "tts_enabled": True,
    "tts_provider": "deepgram",
    "tts_voice": "aura-asteria-en",
    "qgen_model": "gpt-4o-mini",
    "eval_model": "gpt-4o-mini",
}

sessions = {}

# ── Candidate History (file-backed, keyed by email) ──────────────────────
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "candidate_history.json")

def _load_history() -> dict:
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except: pass
    return {}

def _save_history_to_disk():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(candidate_history, f, indent=2, default=str)
    except Exception as e:
        print(f"[History] Save failed: {e}")

candidate_history: dict[str, list[dict]] = _load_history()
print(f"[History] Loaded {sum(len(v) for v in candidate_history.values())} sessions for {len(candidate_history)} candidates")


def save_candidate_session(session):
    """Save completed session to candidate history."""
    email = session.get("resume", {}).get("email", "")
    if not email:
        return
    summary = {
        "session_id": session["id"],
        "date": datetime.utcnow().isoformat(),
        "domain": session.get("resume", {}).get("domain", ""),
        "turns": session.get("turn", 0),
        "topics_asked": [e.get("topic", "") for e in session.get("conversation", []) if e.get("question")],
        "questions_asked": [e.get("question", "") for e in session.get("conversation", []) if e.get("question")],
        "projects": session.get("resume", {}).get("key_projects", []),
        "skills": session.get("resume", {}).get("skills", []),
        "tools": session.get("resume", {}).get("tools", []),
        "weak_signals": [],
    }
    candidate_history.setdefault(email, []).append(summary)
    _save_history_to_disk()
    print(f"[History] Saved for {email}: {summary['turns']} turns, session {summary['session_id'][:8]}")


def get_candidate_previous(email: str) -> list[dict]:
    """Get previous sessions for a returning candidate."""
    return candidate_history.get(email, [])

# ── Auth ─────────────────────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)

def create_token(sub: str, role: str = "admin"):
    return jwt.encode({"sub": sub, "role": role, "exp": datetime.utcnow() + timedelta(hours=8)}, JWT_SECRET, algorithm="HS256")

async def require_admin(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = None
    if credentials:
        token = credentials.credentials
    else:
        token = request.cookies.get("auth_token")
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload
    except JWTError:
        raise HTTPException(401, "Invalid token")

@app.post("/api/login")
async def login(data: dict):
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
        token = create_token(ADMIN_USER)
        return {"token": token, "user": ADMIN_USER}
    raise HTTPException(401, "Invalid credentials")

# ── LLM Routing ──────────────────────────────────────────────────────────

def call_llm(messages, model_id="", temperature=0.5, max_tokens=500):
    """Route to correct LLM: OpenAI, Bedrock, or Grok."""
    model = model_id or RUNTIME_CONFIG["qgen_model"]

    # Grok
    if model.startswith("grok-") and xai_client:
        import httpx
        resp = xai_client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            timeout=httpx.Timeout(15.0),
        )
        return resp.choices[0].message.content.strip()

    # Bedrock (Claude, Llama, Nova, etc.)
    if bedrock_client and (model.startswith("us.") or "anthropic" in model or "amazon" in model or "meta" in model):
        return _call_bedrock(messages, model, temperature, max_tokens)

    # OpenAI (default)
    resp = openai_client.chat.completions.create(
        model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    return resp.choices[0].message.content.strip()


def _call_bedrock(messages, model_id, temperature, max_tokens):
    """Call AWS Bedrock models."""
    is_claude = "anthropic" in model_id.lower()
    system_text, user_text = "", ""
    for msg in messages:
        if msg["role"] == "system": system_text += msg["content"] + "\n"
        elif msg["role"] == "user": user_text += msg["content"] + "\n"
        elif msg["role"] == "assistant": pass

    if is_claude:
        filtered = [m for m in messages if m["role"] != "system"]
        if not filtered:
            filtered = [{"role": "user", "content": system_text.strip()}]
            system_text = ""
        for i, m in enumerate(filtered):
            if isinstance(m.get("content"), str):
                filtered[i] = {"role": m["role"], "content": [{"type": "text", "text": m["content"]}]}
        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens, "temperature": temperature, "messages": filtered}
        if system_text.strip():
            body["system"] = [{"type": "text", "text": system_text.strip(), "cache_control": {"type": "ephemeral"}}]
    else:
        is_llama = "meta" in model_id.lower() or "llama" in model_id.lower()
        is_nova = "amazon" in model_id.lower() or "nova" in model_id.lower()
        if is_llama:
            prompt = ""
            if system_text: prompt += f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system_text.strip()}<|eot_id|>"
            prompt += f"<|start_header_id|>user<|end_header_id|>\n\n{user_text.strip()}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
            body = {"prompt": prompt, "max_gen_len": max_tokens, "temperature": temperature}
        elif is_nova:
            body = {"inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature}, "messages": [{"role": "user", "content": [{"text": user_text.strip()}]}]}
            if system_text.strip(): body["system"] = [{"text": system_text.strip()}]
        else:
            body = {"max_tokens": max_tokens, "temperature": temperature, "messages": messages}

    resp = bedrock_client.invoke_model(modelId=model_id, contentType="application/json", accept="application/json", body=json.dumps(body))
    result_body = json.loads(resp["body"].read())

    if is_claude: return result_body["content"][0]["text"].strip()
    elif "meta" in model_id.lower() or "llama" in model_id.lower(): return result_body.get("generation", "").strip()
    elif "amazon" in model_id.lower() or "nova" in model_id.lower(): return result_body.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "").strip()
    elif "content" in result_body: return result_body["content"][0]["text"].strip()
    elif "choices" in result_body: return result_body["choices"][0].get("message", {}).get("content", result_body["choices"][0].get("text", "")).strip()
    return json.dumps(result_body)


# ── Cerebras (fast, free — for resume parsing) ──────────────────────────

def call_cerebras(messages, temperature=0.5, max_tokens=1000):
    """Fast resume parsing via Cerebras. Falls back to OpenAI."""
    if cerebras_client:
        try:
            resp = cerebras_client.chat.completions.create(
                model="llama3.1-8b", messages=messages,
                temperature=temperature, max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"[Cerebras] Failed, falling back: {e}")
    return call_llm(messages, temperature=temperature, max_tokens=max_tokens)


def safe_json(text: str):
    """Extract JSON from LLM response."""
    text = re.sub(r"```json|```", "", text).strip()
    try: return json.loads(text)
    except: pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try: return json.loads(m.group())
        except: pass
    return None


# ── Resume Parsing ───────────────────────────────────────────────────────

def parse_resume(resume_text: str) -> dict:
    if not resume_text or len(resume_text.strip()) < 20:
        return {}
    prompt = f"""Extract from this resume. Return ONLY valid JSON:
{{"candidate_name":"","email":"","phone":"","level":"fresh_graduate|trained_fresher|experienced_junior|experienced_senior",
"years_experience":0,"skills":[],"tools":[],"key_projects":[],"domain":"","education":""}}

Rules:
- email: extract email address if present, empty string if not found
- phone: extract phone number if present, empty string if not found
- level: 0 years = fresh_graduate, 0-1 year = trained_fresher, 1-3 = experienced_junior, 3+ = experienced_senior
- skills: VLSI/EDA specific only
- tools: EDA tool names (ICC2, PrimeTime, Calibre, Virtuoso, VCS, etc.)
- key_projects: max 5
- domain: physical_design or analog_layout or design_verification

RESUME:
{resume_text[:3000]}

JSON:"""
    for attempt in range(3):
        try:
            raw = call_cerebras([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=500)
            parsed = safe_json(raw)
            if parsed and parsed.get("candidate_name"):
                print(f"[Resume] Parsed on attempt {attempt+1}: {parsed.get('candidate_name')}")
                return parsed
            print(f"[Resume] Attempt {attempt+1}: empty result, retrying...")
        except Exception as e:
            print(f"[Resume] Attempt {attempt+1} failed: {e}")
    return {}


# ── STT ──────────────────────────────────────────────────────────────────

def transcribe_audio(audio_bytes: bytes, ext: str = "webm") -> str:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(audio_bytes); tmp_path = f.name
        with open(tmp_path, "rb") as audio_file:
            response = openai_client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe", file=audio_file, language="en",
            )
        return response.text.strip() if hasattr(response, "text") else str(response).strip()
    except Exception as e:
        print(f"[STT] Error: {e}")
        return ""
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except: pass


# ── TTS ──────────────────────────────────────────────────────────────────

def synthesize_speech(text: str) -> str:
    """Returns base64 encoded audio."""
    if not RUNTIME_CONFIG.get("tts_enabled", True) or not text:
        return ""
    provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
    voice = RUNTIME_CONFIG.get("tts_voice", "aura-asteria-en")

    # Deepgram
    if provider == "deepgram" and DEEPGRAM_API_KEY:
        try:
            r = http_requests.post(f"https://api.deepgram.com/v1/speak?model={voice}",
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000]}, timeout=15)
            r.raise_for_status()
            return base64.b64encode(r.content).decode()
        except Exception as e:
            print(f"[TTS] Deepgram error: {e}")

    # Inworld
    if provider == "inworld" and INWORLD_API_KEY:
        try:
            r = http_requests.post("https://api.inworld.ai/tts/v1/voice",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000], "voiceId": voice or INWORLD_VOICE_ID, "modelId": INWORLD_MODEL_ID}, timeout=15)
            r.raise_for_status()
            data = r.json() if "json" in r.headers.get("content-type", "") else None
            if data: return data.get("audioContent", base64.b64encode(r.content).decode())
            return base64.b64encode(r.content).decode()
        except Exception as e:
            print(f"[TTS] Inworld error: {e}")

    # OpenAI TTS
    if OPENAI_API_KEY:
        try:
            response = openai_client.audio.speech.create(model="tts-1", voice=voice or "nova", input=text[:2000])
            return base64.b64encode(response.content).decode()
        except Exception as e:
            print(f"[TTS] OpenAI error: {e}")

    return ""


# ── Dynamic Interview Prompts ────────────────────────────────────────────

_BASE = """You are Ranjitha, a principal VLSI design engineer. 14 years experience. 9 tapeouts. 200+ interviews.
You are conducting a real technical interview. A conversation between two engineers.

RULES:
- 1 sentence per turn. 8-20 words. Never more than 25.
- React naturally, then ask ONE follow-up. Or just ask.
- Never teach, explain, summarize, or lecture.
- Never say "Great!", "Interesting", "Good point", "Can you elaborate", "Tell me more".
- If they speak another language: "Please answer in English."
- If they pause: "Take your time."
- If they repeat: "Got it. Let's move on."
- Plain spoken text. No markdown. No bullets.

ENDING THE INTERVIEW:
When you have enough signal to assess the candidate, end naturally.
To end, start your response with [END_INTERVIEW] then a brief closing.
Example: "[END_INTERVIEW] That covers what I needed. Thank you for your time."
End IF: enough topics covered, candidate consistently struggling, or strong depth shown.
Do NOT end before turn 8."""

# ── Level-specific behavior ──────────────────────────────────────────────

_LEVEL = {
    "fresh_graduate": """
LEVEL: Fresh Graduate (0 years)
APPROACH:
- Be patient. This may be their first interview.
- Ask concepts and definitions only. No tool commands, no numbers.
- "What is clock skew?" / "Why does matching matter?"
- If they give textbook answers: "Can you explain that in your own words?"
- If they struggle: simplify. "Let's start simpler — what does [term] mean?"
- Don't ask: debug scenarios, tool commands, numerical targets, trade-offs.
- Accept honest "I don't know" — move to another concept.
EXPECT: Basic understanding of VLSI flow and fundamental concepts.
RED FLAG: Can't explain basic terms even after simplification.""",

    "trained_fresher": """
LEVEL: Trained Fresher (0-1 year, training/internship)
APPROACH:
- They know theory but limited hands-on. Test if knowledge is real or memorized.
- "You learned about CTS — what's the first thing you'd check after running it?"
- If they mention a tool: "What did you actually DO with it?"
- If they claim project work: "What was YOUR specific contribution?"
- Be slightly patient but push for understanding beyond textbooks.
- Don't ask: advanced debug, numerical optimization, trade-off analysis.
EXPECT: Concepts with practical awareness. Basic tool names. Flow understanding.
RED FLAG: Claims experience but can't describe what they personally did.""",

    "experienced_junior": """
LEVEL: Junior Engineer (1-3 years)
APPROACH:
- They should have real project stories and tool experience.
- "Walk me through how you handled [X] on your last project."
- Push for specifics: "What command? What was the target? What number?"
- If vague: "Be specific. What was the actual violation you saw?"
- Test ownership: they should say "I did" not "we did."
- If strong on one area: push to edge cases and failures.
- If weak on specifics: they may have observed but not done the work.
EXPECT: Tool names, commands, real numbers from their work, debug steps.
RED FLAG: Says "we did" for everything. No specific numbers or tool details.""",

    "experienced_senior": """
LEVEL: Senior Engineer (3+ years)
APPROACH:
- No tolerance for surface answers. They must demonstrate depth.
- Ask trade-offs: "You chose X over Y. Why?"
- Ask failures: "Tell me about a time the flow failed. What broke?"
- Ask numbers: "What utilization? What skew target? What IR drop budget?"
- Ask debug: "Post-route STA shows -50ps WNS. Walk me through your debug."
- Challenge confident-but-wrong: "Walk me through that step by step."
- If textbook answer: "That's theory. What did YOU see in silicon?"
- Be direct and skeptical. Respect is earned through depth.
EXPECT: Ownership, numbers, trade-offs, debug methodology, tool mastery.
RED FLAG: Confident claims with no specific details. Theory without practice.""",
}

# ── Domain-specific expectations ─────────────────────────────────────────

_DOMAIN = {
    "physical_design": """
DOMAIN: Physical Design
KEY AREAS: floorplanning, placement, CTS, routing, STA, timing closure, IR drop, DRC/LVS.
FRESHER FOCUS: PD flow sequence, what each step does, basic timing concepts.
JUNIOR FOCUS: ICC2 commands, timing reports, congestion handling, basic ECO.
SENIOR FOCUS: MCMM strategy, OCV/AOCV/POCV, useful skew, power grid design, signoff methodology.""",

    "analog_layout": """
DOMAIN: Analog Layout
KEY AREAS: device matching, parasitics, LDE, guard rings, current mirrors, OTA, LDO, bandgap, PLL.
FRESHER FOCUS: CMOS basics, what matching means, layer stack, DRC/LVS concepts.
JUNIOR FOCUS: common centroid, interdigitation, parasitic extraction, Virtuoso usage.
SENIOR FOCUS: Pelgrom model, LDE effects, FinFET layout, post-layout correlation, noise-aware layout.""",

    "design_verification": """
DOMAIN: Design Verification
KEY AREAS: SystemVerilog, UVM, assertions, functional coverage, formal verification, debugging.
FRESHER FOCUS: SV data types, what a testbench is, simulation vs synthesis.
JUNIOR FOCUS: UVM agent structure, writing drivers/monitors, basic coverage, waveform debug.
SENIOR FOCUS: coverage closure, constrained random optimization, UVM RAL, formal property writing, regression strategy.""",
}


def build_interview_prompt(session):
    """Build dynamic prompt based on candidate level + domain + conversation history."""
    resume = session.get("resume", {})
    history = session.get("conversation", [])

    name = resume.get("candidate_name", "Candidate")
    level = resume.get("level", "trained_fresher")
    domain = resume.get("domain", "physical_design")
    years = resume.get("years_experience", 0)
    tools = ", ".join(str(t) for t in resume.get("tools", [])[:5]) or "not specified"
    projects = ", ".join(str(p) for p in resume.get("key_projects", [])[:3]) or "not specified"
    skills = ", ".join(str(s) for s in resume.get("skills", [])[:8]) or "not specified"

    # Build dynamic system prompt
    level_prompt = _LEVEL.get(level, _LEVEL["trained_fresher"])
    domain_prompt = _DOMAIN.get(domain, _DOMAIN["physical_design"])
    candidate_info = f"\nCANDIDATE: {name} | {level.replace('_',' ')} | {years} years | Tools: {tools} | Projects: {projects} | Skills: {skills}"

    # Check for returning candidate
    returning_block = ""
    email = resume.get("email", "")
    if email:
        prev_sessions = get_candidate_previous(email)
        if prev_sessions:
            # Take only last 2 sessions
            recent = prev_sessions[-2:]
            prev_questions = []
            prev_projects = set()
            for ps in recent:
                prev_questions.extend(ps.get("questions_asked", []))
                for p in ps.get("projects", []):
                    prev_projects.add(str(p))

            projects_note = ""
            if prev_projects:
                projects_note = f"\nProjects discussed before: {', '.join(prev_projects)}\nAsk about DIFFERENT aspects of these projects, or explore projects not yet discussed."

            returning_block = f"""
RETURNING CANDIDATE: This candidate has interviewed {len(prev_sessions)} time(s) before.
DO NOT ask these questions again (they may have memorized answers):
{chr(10).join(f'- {q}' for q in prev_questions)}{projects_note}
Ask DIFFERENT questions on DIFFERENT angles of the same topics.
Silently test if they actually improved or just memorized."""

    system = _BASE + level_prompt + domain_prompt + candidate_info + returning_block

    messages = [{"role": "system", "content": system}]

    # Add conversation history
    for entry in history[-10:]:
        if entry.get("question"):
            messages.append({"role": "assistant", "content": entry["question"]})
        if entry.get("answer"):
            messages.append({"role": "user", "content": entry["answer"]})

    return messages


def _get_interview_phase(turn: int) -> str:
    """Determine interview phase based on turn count."""
    if turn <= 1: return "WARM_OPENING"
    elif turn <= 4: return "DISCOVERY"
    else: return "ADAPTIVE_DEPTH"


def _get_topics_covered(session) -> list[str]:
    """Extract topics already covered from conversation."""
    topics = []
    for entry in session.get("conversation", []):
        q = entry.get("question", "").lower()
        for topic in ["floorplan", "placement", "cts", "clock", "routing", "sta", "timing",
                       "ir drop", "power", "drc", "lvs", "matching", "parasitic", "latch",
                       "esd", "ota", "ldo", "bandgap", "pll", "adc", "dac",
                       "uvm", "coverage", "assertion", "sv", "systemverilog", "formal",
                       "debug", "waveform", "verification"]:
            if topic in q and topic not in topics:
                topics.append(topic)
    return topics


def _should_end_interview(session) -> tuple[bool, str]:
    """Hard limit only. Early end decided by LLM via system prompt."""
    if session.get("turn", 0) >= 25:
        return True, "That's all from my side. Thank you for your time."
    return False, ""


def generate_question(session, candidate_answer: str) -> dict:
    """Send conversation + answer to LLM, get next question. LLM handles all intelligence."""

    # Add candidate's answer to history
    if session["conversation"]:
        session["conversation"][-1]["answer"] = candidate_answer

    # Check auto-end
    should_end, end_msg = _should_end_interview(session)
    if should_end:
        session["phase"] = "ended"
        return {"question": end_msg, "should_end": True}

    # Build prompt with pacing + topic context
    messages = build_interview_prompt(session)

    phase = _get_interview_phase(session["turn"])
    topics_covered = _get_topics_covered(session)

    pacing = f"\nPHASE: {phase} | Turn: {session['turn']}"
    if topics_covered:
        pacing += f"\nTopics covered: {', '.join(topics_covered)}. Ask about DIFFERENT topics."

    messages.append({"role": "user", "content": candidate_answer + pacing})

    # Call LLM
    question = call_llm(messages, temperature=0.7, max_tokens=120)

    # Clean markdown
    question = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', question)
    question = re.sub(r'`([^`]+)`', r'\1', question)
    question = re.sub(r'#{1,3}\s*', '', question)

    # Check if LLM decided to end the interview
    llm_end = "[END_INTERVIEW]" in question
    if llm_end:
        question = question.replace("[END_INTERVIEW]", "").strip()
        session["phase"] = "ended"

    # Add to history
    session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
    session["turn"] += 1

    return {"question": question, "should_end": llm_end}


def generate_greeting(session) -> str:
    """Let the LLM generate a natural opening based on candidate context."""
    resume = session.get("resume", {})
    email = resume.get("email", "")
    prev_sessions = get_candidate_previous(email) if email else []

    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    hour = datetime.now(ist).hour
    time_of_day = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"

    # Pick a short callable name from full name
    full_name = resume.get("candidate_name", "") or ""
    parts = full_name.strip().split()
    if len(parts) <= 2:
        call_name = parts[0] if parts else ""
    else:
        # Long name — skip surname (first) and title-like parts, pick the actual first name
        # "Beeram Veera Venkata Reddy" → "Veera"
        # Skip parts that look like surnames (first part) or suffixes (last part)
        call_name = parts[1] if len(parts) > 2 else parts[0]

    context = f"""Generate a natural, warm opening greeting for a technical interview.

Time: {time_of_day}
Candidate name: {call_name}
Domain: {resume.get('domain', 'VLSI').replace('_', ' ')}
Level: {resume.get('level', 'fresher').replace('_', ' ')}
Returning: {'yes, interviewed ' + str(len(prev_sessions)) + ' time(s) before' if prev_sessions else 'no, first time'}

Rules:
- 1-2 sentences only
- Greet naturally, ask them to introduce themselves
- Do NOT ask technical questions yet
- Do NOT mention scoring or evaluation
- If returning: acknowledge briefly, don't reveal previous scores
- Sound like a real person, not a script"""

    try:
        greeting = call_llm([{"role": "user", "content": context}], temperature=0.8, max_tokens=60)
        greeting = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', greeting).strip()
    except:
        name = (resume.get("candidate_name", "") or "").split()[0] if resume.get("candidate_name") else ""
        greeting = f"Hi{' ' + name if name else ''}, thanks for joining. Tell me about yourself."

    if prev_sessions:
        session["is_returning"] = True
        session["previous_sessions"] = len(prev_sessions)

    session["conversation"].append({"question": greeting, "answer": None, "turn": 0})
    return greeting


# ── API Endpoints ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return open("templates/index.html").read()

@app.get("/interview", response_class=HTMLResponse)
async def interview_page():
    return open("templates/voice_agent_ui.html").read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return open("templates/admin.html").read()

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Auth endpoints (matching monolith admin.html) ────────────────────────

@app.post("/api/auth/login")
async def auth_login(data: dict, response: Response):
    if data.get("username") == ADMIN_USER and data.get("password") == ADMIN_PASS:
        token = create_token(ADMIN_USER)
        response.set_cookie("auth_token", token, httponly=True, max_age=28800)
        return {"token": token, "user": ADMIN_USER, "role": "admin"}
    raise HTTPException(401, "Invalid credentials")

@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("auth_token")
    return {"ok": True}

@app.get("/api/auth/me")
async def auth_me(request: Request):
    token = request.cookies.get("auth_token")
    if not token:
        cred = request.headers.get("Authorization", "")
        if cred.startswith("Bearer "): token = cred[7:]
    if not token: raise HTTPException(401)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        username = payload["sub"]
        role = payload.get("role", "admin")
        return {"user": username, "username": username, "role": role}
    except: raise HTTPException(401)

@app.post("/api/login")
async def login(data: dict, response: Response):
    return await auth_login(data, response)


# ── Resume Parsing ───────────────────────────────────────────────────────

@app.post("/api/parse-resume")
async def parse_resume_endpoint(file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > 5_000_000:
        raise HTTPException(413, "File too large. Max 5MB.")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "txt"

    if ext == "pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(400, "Not a valid PDF.")
        try:
            import pdfplumber
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content); tmp_path = tmp.name
            text = ""
            with pdfplumber.open(tmp_path) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
            os.unlink(tmp_path)
        except Exception as e:
            raise HTTPException(400, f"PDF error: {e}")
    elif ext in ("docx", "doc"):
        try:
            import docx2txt
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                tmp.write(content); tmp_path = tmp.name
            text = docx2txt.process(tmp_path)
            os.unlink(tmp_path)
        except Exception as e:
            raise HTTPException(400, f"DOCX error: {e}")
    else:
        text = content.decode("utf-8", errors="ignore")

    if not text.strip():
        return {"is_vlsi_suitable": False, "rejection_reason": "Could not read resume file."}

    parsed = parse_resume(text)
    parsed["is_vlsi_suitable"] = True
    parsed["resume_text"] = text[:3000]
    return JSONResponse(parsed)


# ── Session Management ───────────────────────────────────────────────────

@app.post("/api/create-session")
async def create_session_endpoint(data: dict):
    resume_text = data.get("resume_text", "")
    mode = data.get("mode", "mock")
    domain = data.get("domain", "physical_design")

    resume = {}
    if resume_text:
        # Frontend sends JSON.stringify(parsedResume) — try to use it directly
        try:
            resume = json.loads(resume_text)
        except (json.JSONDecodeError, TypeError):
            # Raw text — parse it
            resume = parse_resume(resume_text)
    if not resume.get("domain"):
        resume["domain"] = domain

    sid = secrets.token_hex(8)
    session = {
        "id": sid, "mode": mode, "resume": resume, "phase": "greeting",
        "turn": 0, "conversation": [], "started_at": time.time(),
        "difficulty_level": 1,
    }
    sessions[sid] = session
    return {"session_id": sid, "resume": resume}


@app.post("/api/start-interview")
async def start_interview(data: dict):
    sid = data.get("session_id")
    session = sessions.get(sid)
    if not session: raise HTTPException(404, "Session not found")

    greeting = generate_greeting(session)
    audio = synthesize_speech(greeting)
    session["phase"] = "interview"

    return {
        "question": greeting, "question_type": "greeting", "turn": session["turn"],
        "phase": session["phase"], "audio": audio, "difficulty": "basic",
        "should_end": False, "resume": session.get("resume", {}),
    }


@app.post("/api/transcribe")
async def transcribe_endpoint(audio: UploadFile = File(...), session_id: str = Form("")):
    audio_bytes = await audio.read()
    ext = audio.filename.rsplit(".", 1)[-1] if audio.filename else "webm"
    transcript = transcribe_audio(audio_bytes, ext)
    return {"transcript": transcript}


@app.post("/api/submit-answer")
async def submit_answer(data: dict):
    sid = data.get("session_id")
    answer = data.get("answer", "")
    session = sessions.get(sid)
    if not session: raise HTTPException(404, "Session not found")

    result = generate_question(session, answer)
    audio = synthesize_speech(result["question"])

    if result["should_end"]:
        session["phase"] = "ended"
        save_candidate_session(session)

    return {
        "question": result["question"], "question_type": "interview",
        "turn": session["turn"], "phase": session["phase"],
        "audio": audio, "difficulty": "basic",
        "should_end": result["should_end"],
    }


@app.post("/api/end-session")
async def end_session(data: dict):
    sid = data.get("session_id")
    session = sessions.get(sid)
    if session:
        session["phase"] = "ended"
        save_candidate_session(session)
    return {"ok": True}


@app.get("/api/get-session")
async def get_session_endpoint(session_id: str):
    session = sessions.get(session_id)
    if not session: raise HTTPException(404, "Session not found")
    return {"session_id": session_id, "phase": session["phase"], "turn": session["turn"],
            "resume": session.get("resume", {}), "mode": session.get("mode", "mock")}


@app.post("/api/generate-report")
async def generate_report(data: dict):
    sid = data.get("session_id")
    session = sessions.get(sid)
    if not session: return {"report": "No session found."}
    return {"report": "Interview completed.", "session_id": sid, "turns": session["turn"]}


# ── Anti-cheat stubs (templates call these) ──────────────────────────────

@app.post("/api/anticheat-event")
async def anticheat_event(data: dict):
    return {"ok": True}

@app.get("/api/anticheat-settings")
async def anticheat_settings():
    return {"face_detect": False, "head_turn": False, "eye_away": False}

@app.post("/api/sim/ai-done")
async def sim_ai_done(data: dict):
    return {"ok": True}


# ── Admin: LLM Config ───────────────────────────────────────────────────

AVAILABLE_MODELS = [
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "tier": "fast", "input_cost": "$0.15/1M", "output_cost": "$0.60/1M", "latency": "~1-2s", "context": "128K", "best_for": "Fast, cheap"},
    {"id": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "name": "Claude Haiku 4.5", "tier": "fast", "input_cost": "$1.00/1M", "output_cost": "$5.00/1M", "latency": "~0.5-1s", "context": "200K", "best_for": "Question generation"},
    {"id": "us.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "tier": "balanced", "input_cost": "$3.00/1M", "output_cost": "$15.00/1M", "latency": "~1-2s", "context": "200K", "best_for": "Evaluation"},
    {"id": "grok-4-1-fast-non-reasoning", "name": "Grok 4.1 Fast", "tier": "fast", "input_cost": "$0.20/1M", "output_cost": "$0.50/1M", "latency": "~0.3-0.5s", "context": "2M", "best_for": "Fastest"},
    {"id": "us.meta.llama4-maverick-17b-instruct-v1:0", "name": "Llama 4 Maverick", "tier": "fast", "input_cost": "$0.17/1M", "output_cost": "$0.17/1M", "latency": "~0.5-1s", "context": "128K", "best_for": "Cheapest"},
    {"id": "us.amazon.nova-lite-v1:0", "name": "Amazon Nova Lite", "tier": "fast", "input_cost": "$0.06/1M", "output_cost": "$0.24/1M", "latency": "~0.5s", "context": "300K", "best_for": "AWS native"},
]

@app.get("/api/admin/llm-config")
async def get_llm_config(_=Depends(require_admin)):
    return {"qgen_model": RUNTIME_CONFIG["qgen_model"], "eval_model": RUNTIME_CONFIG["eval_model"], "available_models": AVAILABLE_MODELS}

@app.post("/api/admin/llm-config")
async def set_llm_config(data: dict, _=Depends(require_admin)):
    if "qgen_model" in data: RUNTIME_CONFIG["qgen_model"] = data["qgen_model"]
    if "eval_model" in data: RUNTIME_CONFIG["eval_model"] = data["eval_model"]
    return {"status": "success", "qgen_model": RUNTIME_CONFIG["qgen_model"], "eval_model": RUNTIME_CONFIG["eval_model"]}

@app.get("/api/admin/llm-prompts")
async def get_llm_prompts(_=Depends(require_admin)):
    return {"eval_prompt": "", "qgen_rules": "", "qgen_prompt": INTERVIEWER_PROMPT}

@app.post("/api/admin/llm-prompts")
async def set_llm_prompts(data: dict, _=Depends(require_admin)):
    return {"status": "success"}

@app.get("/api/admin/qgen-prompt")
async def get_qgen_prompt(domain: str = "physical_design", level: str = "trained_fresher", name: str = "Sample", _=Depends(require_admin)):
    return {"prompt": INTERVIEWER_PROMPT}


# ── Admin: Voice Config ──────────────────────────────────────────────────

@app.post("/api/toggle-tts")
async def toggle_tts(data: dict):
    RUNTIME_CONFIG["tts_enabled"] = data.get("enabled", True)
    return {"ok": True, "tts_enabled": RUNTIME_CONFIG["tts_enabled"]}

@app.get("/api/tts-status")
async def tts_status():
    return {"tts_enabled": RUNTIME_CONFIG["tts_enabled"]}

@app.post("/api/admin/set-interview-voice")
async def set_interview_voice(data: dict, _=Depends(require_admin)):
    RUNTIME_CONFIG["tts_provider"] = data.get("provider", "deepgram")
    RUNTIME_CONFIG["tts_voice"] = data.get("voice", "")
    return {"ok": True, "provider": RUNTIME_CONFIG["tts_provider"], "voice": RUNTIME_CONFIG["tts_voice"]}

@app.post("/api/admin/test-tts")
async def test_tts(data: dict, _=Depends(require_admin)):
    text = data.get("text", "")
    provider = data.get("provider", RUNTIME_CONFIG["tts_provider"])
    voice = data.get("voice", RUNTIME_CONFIG["tts_voice"])
    old_p, old_v = RUNTIME_CONFIG["tts_provider"], RUNTIME_CONFIG["tts_voice"]
    RUNTIME_CONFIG["tts_provider"], RUNTIME_CONFIG["tts_voice"] = provider, voice
    t0 = time.time()
    audio = synthesize_speech(text)
    RUNTIME_CONFIG["tts_provider"], RUNTIME_CONFIG["tts_voice"] = old_p, old_v
    return {"audio": audio, "latency_ms": int((time.time() - t0) * 1000)}

@app.get("/api/admin/voice-library")
async def voice_library(_=Depends(require_admin)):
    return {"voices": []}

@app.post("/api/admin/clone-voice")
async def clone_voice(_=Depends(require_admin)):
    return {"ok": False, "error": "Not supported in simple mode"}

@app.post("/api/playground/tts")
async def playground_tts(data: dict):
    text = data.get("text", "")
    audio = synthesize_speech(text)
    return {"audio": audio}


# ── Admin: Prompt Playground ─────────────────────────────────────────────

@app.post("/api/admin/prompt-playground")
async def prompt_playground(data: dict, _=Depends(require_admin)):
    prompt_text = data.get("prompt", "")
    model_id = data.get("model_id", RUNTIME_CONFIG["qgen_model"])
    temperature = data.get("temperature", 0.3)
    max_tokens = data.get("max_tokens", 600)
    system_prompt = data.get("system_prompt", "")

    msgs = []
    if system_prompt: msgs.append({"role": "system", "content": system_prompt})
    msgs.append({"role": "user", "content": prompt_text})

    t0 = time.time()
    try:
        raw = call_llm(msgs, model_id=model_id, temperature=temperature, max_tokens=max_tokens)
        return {"status": "success", "raw_response": raw, "latency_ms": round((time.time() - t0) * 1000), "model": model_id}
    except Exception as e:
        return {"status": "error", "error": str(e), "latency_ms": round((time.time() - t0) * 1000), "model": model_id}


# ── Admin: Anti-cheat Config (stubs) ─────────────────────────────────────

@app.get("/api/admin/anticheat-config")
async def get_anticheat_config(_=Depends(require_admin)):
    return {"features": {}}

@app.post("/api/admin/anticheat-config")
async def set_anticheat_config(data: dict, _=Depends(require_admin)):
    return {"status": "success"}


# ── Admin: Sessions ──────────────────────────────────────────────────────

@app.get("/api/admin/sessions")
async def admin_sessions(_=Depends(require_admin)):
    session_list = []
    for sid, s in sessions.items():
        session_list.append({
            "session_id": sid,
            "id": sid,
            "resume": s.get("resume", {}),
            "phase": s.get("phase", ""),
            "turn": s.get("turn", 0),
            "mode": s.get("mode", "mock"),
            "started_at": s.get("started_at", 0),
            "difficulty_level": s.get("difficulty_level", 1),
        })
    return session_list


@app.get("/api/admin/session/{sid}")
async def admin_session_detail(sid: str, _=Depends(require_admin)):
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    turn_log = []
    for entry in session.get("conversation", []):
        turn_log.append({
            "turn": entry.get("turn", 0),
            "phase": "interview",
            "question": entry.get("question", ""),
            "answer": entry.get("answer", ""),
            "question_type": "interview",
            "topic": "",
            "difficulty": "basic",
            "score": "",
            "quality": "",
            "accuracy": "",
            "quadrant": "",
            "notes": "",
            "word_count": len((entry.get("answer") or "").split()),
            "answer_duration_sec": 0,
            "score_reasoning": "",
            "expected_points": [],
            "missing_points": [],
            "level_gap": 0,
            "behavioral_flags": [],
        })
    return {
        "session_id": sid,
        "resume": session.get("resume", {}),
        "phase": session.get("phase", ""),
        "turn": session.get("turn", 0),
        "difficulty_level": session.get("difficulty_level", 1),
        "trajectory": "unknown",
        "turn_log": turn_log,
        "anticheat_log": [],
        "contradiction_log": [],
        "recovery_log": [],
        "notable_moments": [],
        "genuine_signals": [],
        "suspicion_events": [],
        "raw_scores": [],
        "topics_covered": [],
        "expert_reviews": [],
        "eval_model": RUNTIME_CONFIG.get("eval_model", "gpt-4o-mini"),
        "qgen_model": RUNTIME_CONFIG.get("qgen_model", "gpt-4o-mini"),
        "observability": {"session_id": sid, "total_calls": 0, "logs": []},
    }


# ── Admin: Observability ─────────────────────────────────────────────────

@app.get("/api/observability/summary")
async def obs_summary(window: int = 86400, _=Depends(require_admin)):
    return {
        "total_calls": 0, "success_calls": 0, "failure_calls": 0,
        "total_cost_usd": 0, "avg_latency_ms": 0,
        "step_breakdown": {},
    }

@app.get("/api/observability/logs")
async def obs_logs(limit: int = 500, _=Depends(require_admin)):
    return []


# ── Admin: Expert Review ─────────────────────────────────────────────────

@app.post("/api/admin/review")
async def submit_review(data: dict, _=Depends(require_admin)):
    return {"ok": True, "review_id": f"R-{secrets.token_hex(4).upper()}"}


# ── Start ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"  LLM: {RUNTIME_CONFIG['qgen_model']}")
    print(f"  TTS: {RUNTIME_CONFIG['tts_provider']} / {RUNTIME_CONFIG['tts_voice']}")
    print(f"  STT: gpt-4o-mini-transcribe")
    print(f"  Bedrock: {'ready' if bedrock_client else 'not configured'}")
    print(f"  Grok: {'ready' if xai_client else 'not configured'}")
    uvicorn.run(app, host="0.0.0.0", port=8001)
