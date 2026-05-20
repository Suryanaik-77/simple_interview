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
{{"candidate_name":"","level":"fresh_graduate|trained_fresher|experienced_junior|experienced_senior",
"years_experience":0,"skills":[],"tools":[],"key_projects":[],"domain":"","education":""}}

Rules:
- level: 0 years = fresh_graduate, 0-1 year = trained_fresher, 1-3 = experienced_junior, 3+ = experienced_senior
- skills: VLSI/EDA specific only
- tools: EDA tool names (ICC2, PrimeTime, Calibre, Virtuoso, VCS, etc.)
- key_projects: max 5
- domain: physical_design or analog_layout or design_verification

RESUME:
{resume_text[:3000]}

JSON:"""
    try:
        raw = call_cerebras([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=500)
        parsed = safe_json(raw)
        return parsed if parsed else {}
    except Exception as e:
        print(f"[Resume] Parse failed: {e}")
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


# ── Interview System Prompt ──────────────────────────────────────────────

INTERVIEWER_PROMPT = """You are Ranjitha, a principal VLSI design engineer with 14 years of experience. You've taped out 9 chips and interviewed over 200 candidates.

You are conducting a real technical interview right now.

BEHAVIOR:
- React briefly to what the candidate said, then ask ONE follow-up question.
- If their answer is correct: push deeper.
- If wrong: correct them directly, then redirect.
- If vague: call it out — "That's textbook. What did YOU actually do?"
- If they say "I don't know": acknowledge briefly, ask something simpler.
- If they speak in a different language: tell them to speak in English.
- Never say "Great!", "Interesting", "Good point", "Can you elaborate".
- Sound like a real person, not a chatbot.

TONE: Direct. Skeptical. Like a senior engineer with limited patience.
LENGTH: 1-2 sentences. Reaction + question.
FORMAT: Plain text. No markdown, no bullets, no labels."""


def build_interview_prompt(session):
    """Build the full prompt with resume + conversation history."""
    resume = session.get("resume", {})
    history = session.get("conversation", [])

    # Resume context
    name = resume.get("candidate_name", "Candidate")
    domain = resume.get("domain", "VLSI").replace("_", " ")
    level = resume.get("level", "fresher").replace("_", " ")
    years = resume.get("years_experience", 0)
    tools = ", ".join(str(t) for t in resume.get("tools", [])[:5]) or "not specified"
    projects = ", ".join(str(p) for p in resume.get("key_projects", [])[:3]) or "not specified"
    skills = ", ".join(str(s) for s in resume.get("skills", [])[:8]) or "not specified"

    candidate_info = f"CANDIDATE: {name} | {level} | {years} years | Domain: {domain} | Tools: {tools} | Projects: {projects} | Skills: {skills}"

    messages = [{"role": "system", "content": INTERVIEWER_PROMPT + "\n\n" + candidate_info}]

    # Add conversation history
    for entry in history[-10:]:  # last 10 exchanges
        if entry.get("question"):
            messages.append({"role": "assistant", "content": entry["question"]})
        if entry.get("answer"):
            messages.append({"role": "user", "content": entry["answer"]})

    return messages


def generate_question(session, candidate_answer: str) -> str:
    """Simple: send conversation + answer to LLM, get next question."""
    # Add candidate's answer to history
    if session["conversation"]:
        session["conversation"][-1]["answer"] = candidate_answer

    messages = build_interview_prompt(session)
    if candidate_answer:
        messages.append({"role": "user", "content": candidate_answer})

    # Call LLM
    question = call_llm(messages, temperature=0.7, max_tokens=150)

    # Clean markdown
    question = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', question)
    question = re.sub(r'`([^`]+)`', r'\1', question)
    question = re.sub(r'#{1,3}\s*', '', question)

    # Add to history
    session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
    session["turn"] += 1

    return question


def generate_greeting(session) -> str:
    """Generate opening question."""
    resume = session.get("resume", {})
    name = resume.get("candidate_name", "Candidate")
    if name and name != "Candidate":
        name = name.split()[0]
    domain = resume.get("domain", "VLSI").replace("_", " ")

    greeting = f"Hi {name}, let's get started. Tell me about the most challenging {domain} project you've worked on — what was the main technical difficulty?"
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

    question = generate_question(session, answer)
    audio = synthesize_speech(question)

    should_end = session["turn"] >= 20

    return {
        "question": question, "question_type": "interview", "turn": session["turn"],
        "phase": session["phase"], "audio": audio, "difficulty": "basic",
        "should_end": should_end,
    }


@app.post("/api/end-session")
async def end_session(data: dict):
    sid = data.get("session_id")
    session = sessions.get(sid)
    if session: session["phase"] = "ended"
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
