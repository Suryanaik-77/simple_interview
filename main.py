"""
Simple Interview Agent — Voice AI Mock Interview
Everything from the monolith EXCEPT: strategy engine, repetition guard,
question pipeline, evaluation pipeline, evaluation validator.

Question generation: conversation history + resume → LLM → next question.
That's it. No complex routing.
"""
import os, time, json, re, secrets, tempfile, base64, threading, smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, UploadFile, HTTPException, Form, Depends, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "gsuryanaik7@gmail.com")
SAPLING_API_KEY = os.getenv("SAPLING_API_KEY", "")

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
    "stt_provider": "openai",
    "stt_model": "gpt-4o-mini-transcribe",
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
        "date": datetime.now(tz=timezone.utc).isoformat(),
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
    return jwt.encode({"sub": sub, "role": role, "exp": datetime.now(tz=timezone.utc) + timedelta(hours=8)}, JWT_SECRET, algorithm="HS256")

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

# ── Cost Tracking ────────────────────────────────────────────────────────

# Pricing per 1M tokens (input, output) — keep in sync with AVAILABLE_MODELS
_LLM_PRICING = {
    "gpt-4o-mini":                              (0.15, 0.60),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.00, 5.00),
    "grok-4-1-fast-non-reasoning":              (0.20, 0.50),
    "us.meta.llama4-maverick-17b-instruct-v1:0":(0.17, 0.17),
    "us.meta.llama4-scout-17b-instruct-v1:0":   (0.17, 0.17),
    "us.amazon.nova-lite-v1:0":                 (0.06, 0.24),
    "us.amazon.nova-micro-v1:0":                (0.035, 0.14),
    "us.amazon.nova-pro-v1:0":                  (0.80, 3.20),
    "us.meta.llama3-3-70b-instruct-v1:0":       (0.72, 0.72),
    "us.anthropic.claude-sonnet-4-6":           (3.00, 15.00),
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": (3.00, 15.00),
    "us.anthropic.claude-opus-4-6-v1":          (15.00, 75.00),
    "us.anthropic.claude-opus-4-5-20251101-v1:0": (15.00, 75.00),
    "us.deepseek.r1-v1:0":                     (1.35, 5.40),
    "grok-4-1-fast-reasoning":                  (0.20, 0.50),
}

# STT pricing per minute
_STT_PRICING = {
    "gpt-4o-mini-transcribe": 0.006,
    "whisper-1": 0.006,
    "nova-3": 0.0059,
    "nova-2": 0.0043,
}

# TTS pricing per 1K characters (estimates)
_TTS_PRICING = {
    "deepgram": 0.015,
    "inworld": 0.015,
    "openai": 0.015,
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return max(1, len(text) // 4)


def _estimate_message_tokens(messages: list) -> int:
    """Estimate input tokens from a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                total += _estimate_tokens(block.get("text", ""))
        total += 4  # role + formatting overhead
    return total


def _calc_llm_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate LLM cost in USD."""
    pricing = _LLM_PRICING.get(model, (0.15, 0.60))  # default to gpt-4o-mini
    return (input_tokens * pricing[0] + output_tokens * pricing[1]) / 1_000_000


def _calc_stt_cost(model: str, duration_ms: int) -> float:
    """Calculate STT cost in USD. duration_ms is audio length, not latency."""
    rate = _STT_PRICING.get(model, 0.006)
    # We don't know exact audio duration, estimate from latency (rough: latency ≈ 0.5-1x audio length)
    # Better: use actual audio duration if available
    minutes = duration_ms / 60000
    return minutes * rate


def _calc_tts_cost(provider: str, chars: int) -> float:
    """Calculate TTS cost in USD."""
    rate = _TTS_PRICING.get(provider, 0.015)
    return (chars / 1000) * rate


def _obs_entry(step: str, model: str, latency_ms: int, status: str = "success",
               input_tokens: int = 0, output_tokens: int = 0, chars: int = 0,
               cost_usd: float = 0.0, error: str = "", **extra) -> dict:
    """Build a standardized obs_log entry."""
    entry = {
        "step": step,
        "model": model,
        "latency_ms": latency_ms,
        "status": status,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "chars": chars,
        "cost_usd": round(cost_usd, 6),
        "ts": time.time(),
    }
    if error:
        entry["error"] = error
    entry.update(extra)
    return entry


# ── LLM Routing ──────────────────────────────────────────────────────────

def call_llm(messages, model_id="", temperature=0.5, max_tokens=500):
    """Route to correct LLM: OpenAI, Bedrock, or Grok.
    Returns (text, usage_dict) where usage_dict has input_tokens, output_tokens, cost_usd."""
    model = model_id or RUNTIME_CONFIG["qgen_model"]
    input_est = _estimate_message_tokens(messages)

    # Grok
    if model.startswith("grok-") and xai_client:
        import httpx
        resp = xai_client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            timeout=httpx.Timeout(15.0),
        )
        text = resp.choices[0].message.content.strip()
        in_tok = getattr(resp.usage, "prompt_tokens", input_est) if resp.usage else input_est
        out_tok = getattr(resp.usage, "completion_tokens", _estimate_tokens(text)) if resp.usage else _estimate_tokens(text)
        return text, {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": _calc_llm_cost(model, in_tok, out_tok)}

    # Bedrock (Claude, Llama, Nova, etc.)
    if bedrock_client and (model.startswith("us.") or "anthropic" in model or "amazon" in model or "meta" in model):
        text = _call_bedrock(messages, model, temperature, max_tokens)
        out_tok = _estimate_tokens(text)
        return text, {"input_tokens": input_est, "output_tokens": out_tok, "cost_usd": _calc_llm_cost(model, input_est, out_tok)}

    # OpenAI (default)
    resp = openai_client.chat.completions.create(
        model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    text = resp.choices[0].message.content.strip()
    in_tok = resp.usage.prompt_tokens if resp.usage else input_est
    out_tok = resp.usage.completion_tokens if resp.usage else _estimate_tokens(text)
    return text, {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": _calc_llm_cost(model, in_tok, out_tok)}


def stream_llm(messages, model_id="", temperature=0.5, max_tokens=500):
    """Stream LLM tokens. Yields text chunks. Works with OpenAI and Grok."""
    model = model_id or RUNTIME_CONFIG["qgen_model"]

    # Grok streaming
    if model.startswith("grok-") and xai_client:
        import httpx
        stream = xai_client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True, timeout=httpx.Timeout(15.0),
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        return

    # Bedrock — no streaming support in current code, fall back to full response
    if bedrock_client and (model.startswith("us.") or "anthropic" in model or "amazon" in model or "meta" in model):
        full = _call_bedrock(messages, model, temperature, max_tokens)
        # Simulate streaming by yielding sentence by sentence
        for sent in re.split(r'(?<=[.?!])\s+', full):
            if sent.strip():
                yield sent.strip() + " "
        return

    # OpenAI streaming
    stream = openai_client.chat.completions.create(
        model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
        stream=True,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def tts_chunk(text: str) -> bytes:
    """Generate TTS audio bytes for a text chunk. Returns raw audio bytes."""
    if not RUNTIME_CONFIG.get("tts_enabled", True) or not text.strip():
        return b""
    provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
    voice = RUNTIME_CONFIG.get("tts_voice", "aura-asteria-en")

    if provider == "deepgram" and DEEPGRAM_API_KEY:
        try:
            r = http_requests.post(f"https://api.deepgram.com/v1/speak?model={voice}",
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000]}, timeout=15)
            r.raise_for_status()
            return r.content
        except Exception as e:
            print(f"[TTS Stream] Deepgram error: {e}")

    if provider == "inworld" and INWORLD_API_KEY:
        try:
            r = http_requests.post("https://api.inworld.ai/tts/v1/voice",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000], "voiceId": voice or INWORLD_VOICE_ID, "modelId": INWORLD_MODEL_ID}, timeout=15)
            r.raise_for_status()
            data = r.json() if "json" in r.headers.get("content-type", "") else None
            if data and data.get("audioContent"):
                return base64.b64decode(data["audioContent"])
            return r.content
        except Exception as e:
            print(f"[TTS Stream] Inworld error: {e}")

    if OPENAI_API_KEY:
        try:
            response = openai_client.audio.speech.create(model="tts-1", voice=voice or "nova", input=text[:2000])
            return response.content
        except Exception as e:
            print(f"[TTS Stream] OpenAI error: {e}")

    return b""


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
    text, _usage = call_llm(messages, temperature=temperature, max_tokens=max_tokens)
    return text


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
- years_experience
  Convert ALL experience into decimal years
  Examples:
  - 6 months = 0.5
  - 10 months = 0.8
  - 1 year 6 months = 1.5
  - 2 years 3 months = 2.25
  NEVER confuse months with years
  Freshers = 0
- skills: VLSI/EDA specific only
- tools: EDA tool names (ICC2, PrimeTime, Calibre, Virtuoso, VCS, etc.)
- key_projects: max 5
- domain: physical_design or analog_layout or design_verification

RESUME:
{resume_text[:3000]}

JSON:"""
    haiku_model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    for attempt in range(3):
        try:
            raw, usage = call_llm([{"role": "user", "content": prompt}],
                                  model_id=haiku_model, temperature=0.1, max_tokens=500)
            parsed = safe_json(raw)
            if parsed and parsed.get("candidate_name"):
                print(f"[Resume] Parsed on attempt {attempt+1}: {parsed.get('candidate_name')} | ${usage['cost_usd']:.4f}")
                return parsed
            print(f"[Resume] Attempt {attempt+1}: empty result, retrying...")
        except Exception as e:
            print(f"[Resume] Attempt {attempt+1} failed: {e}")
    return {}


# ── STT ──────────────────────────────────────────────────────────────────

# VLSI domain keywords for STT accuracy boosting
VLSI_KEYWORDS = [
    # Physical Design
    "ICC2", "PrimeTime", "Calibre", "Innovus", "Genus", "Design Compiler",
    "floorplan", "floorplanning", "placement", "routing", "CTS", "clock tree synthesis",
    "STA", "static timing analysis", "DRC", "LVS", "DFM", "OPC",
    "setup time", "hold time", "slack", "WNS", "TNS", "skew",
    "IR drop", "EM", "electromigration", "decap", "power grid",
    "OCV", "AOCV", "POCV", "MMMC", "MCMM", "signoff",
    "ECO", "buffer insertion", "cell swapping", "useful skew",
    "congestion", "utilization", "blockage", "macro", "standard cell",
    "NDR", "antenna", "via", "metal layer", "GDSII", "LEF", "DEF",
    "netlist", "synthesis", "tapeout", "PDK",
    # Analog Layout
    "Virtuoso", "Spectre", "Assura", "PEX", "Cadence",
    "matching", "common centroid", "interdigitation", "guard ring",
    "parasitic", "parasitic extraction", "LDE", "STI stress", "WPE",
    "Pelgrom", "FinFET", "CMOS", "NMOS", "PMOS",
    "current mirror", "OTA", "LDO", "bandgap", "PLL", "ADC", "DAC",
    "latch-up", "ESD", "substrate noise", "shielding",
    # Design Verification
    "SystemVerilog", "UVM", "UVM RAL", "SVA", "assertions",
    "VCS", "Questa", "Xcelium", "JasperGold",
    "testbench", "driver", "monitor", "sequencer", "scoreboard",
    "functional coverage", "code coverage", "coverpoint", "cross coverage",
    "constrained random", "formal verification", "simulation",
    "AXI", "AHB", "APB", "PCIe", "DDR", "AMBA",
    "regression", "waveform", "debug",
]

# Deepgram keywords format: "word:boost" (boost 1-10)
_DG_KEYWORDS = "&".join(f"keywords={k}:5" for k in VLSI_KEYWORDS[:50])

# OpenAI prompt hint for VLSI domain
_OPENAI_STT_PROMPT = (
    "This is a VLSI semiconductor technical interview. "
    "Common terms: ICC2, PrimeTime, Calibre, Innovus, Virtuoso, VCS, Questa, "
    "CTS, STA, DRC, LVS, OCV, AOCV, POCV, MMMC, ECO, NDR, GDSII, LEF, DEF, "
    "floorplanning, placement, routing, clock tree synthesis, static timing analysis, "
    "setup time, hold time, slack, WNS, TNS, IR drop, electromigration, "
    "SystemVerilog, UVM, UVM RAL, SVA, testbench, constrained random, "
    "common centroid, interdigitation, guard ring, Pelgrom, FinFET, LDE, "
    "parasitic extraction, bandgap, PLL, OTA, LDO, current mirror, "
    "AXI, AHB, APB, PCIe, DDR, tapeout, PDK, signoff."
)


def transcribe_audio(audio_bytes: bytes, ext: str = "webm") -> tuple[str, int]:
    """Returns (transcript, latency_ms). Supports OpenAI and Deepgram STT with VLSI keyword boosting."""
    provider = RUNTIME_CONFIG.get("stt_provider", "openai")
    model = RUNTIME_CONFIG.get("stt_model", "gpt-4o-mini-transcribe")
    tmp_path = None
    t0 = time.time()

    # Deepgram STT with keyword boosting
    if provider == "deepgram" and DEEPGRAM_API_KEY:
        try:
            url = f"https://api.deepgram.com/v1/listen?model={model}&language=en&smart_format=true&{_DG_KEYWORDS}"
            r = http_requests.post(url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": f"audio/{ext}"},
                data=audio_bytes, timeout=15)
            r.raise_for_status()
            data = r.json()
            text = data.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "").strip()
            latency = round((time.time() - t0) * 1000)
            print(f"[STT] Deepgram/{model} {latency}ms — {len(text)} chars")
            return text, latency
        except Exception as e:
            print(f"[STT] Deepgram error: {e}, falling back to OpenAI")

    # OpenAI STT with VLSI domain prompt
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(audio_bytes); tmp_path = f.name
        with open(tmp_path, "rb") as audio_file:
            response = openai_client.audio.transcriptions.create(
                model=model if provider == "openai" else "gpt-4o-mini-transcribe",
                file=audio_file, language="en",
                prompt=_OPENAI_STT_PROMPT,
            )
        latency = round((time.time() - t0) * 1000)
        text = response.text.strip() if hasattr(response, "text") else str(response).strip()
        print(f"[STT] OpenAI/{model} {latency}ms — {len(text)} chars")
        return text, latency
    except Exception as e:
        print(f"[STT] Error: {e}")
        return "", round((time.time() - t0) * 1000)
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except: pass


# ── Silero VAD ───────────────────────────────────────────────────────────

_silero_model = None
_silero_utils = None

def _load_silero():
    """Load Silero VAD model (once). Returns (model, get_speech_timestamps, read_audio) or None."""
    global _silero_model, _silero_utils
    if _silero_model is not None:
        return _silero_model, _silero_utils
    try:
        import torch
        model, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad', model='silero_vad', trust_repo=True)
        _silero_model = model
        _silero_utils = utils
        print("[Silero] VAD model loaded")
        return model, utils
    except Exception as e:
        print(f"[Silero] Not available: {e}. Install: pip install torch torchaudio")
        return None, None


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    """WebSocket for real-time audio streaming with Silero VAD.

    Client sends: binary audio chunks (PCM16, 16kHz, mono)
    Server sends: JSON events:
      {"event": "silence_1s"}      → 1.0s silence after speech (speculative STT triggered)
      {"event": "silence_final"}   → 2.0s silence (final, submit answer)
      {"event": "speech_start"}    → candidate started speaking
      {"event": "stt_result", "transcript": "...", "stt_ms": 300, "speculative": true}
    """
    await ws.accept()
    sid = ws.query_params.get("session_id", "")

    # Try loading Silero
    model, utils = _load_silero()
    use_silero = model is not None

    if use_silero:
        import torch
        # Silero VAD state
        vad_sr = 16000
        window_size = 512  # 32ms at 16kHz
        speech_detected = False
        silence_start = None
        speculative_sent = False
        audio_buffer = bytearray()
        all_audio = bytearray()

    await ws.send_json({"event": "connected", "vad": "silero" if use_silero else "browser"})
    print(f"[WS] Audio stream connected (session={sid}, vad={'silero' if use_silero else 'browser'})")

    try:
        while True:
            data = await ws.receive_bytes()

            if not use_silero:
                # No Silero — just accumulate, let browser handle VAD
                continue

            import torch
            all_audio.extend(data)
            audio_buffer.extend(data)

            # Process in 32ms windows
            while len(audio_buffer) >= window_size * 2:  # 2 bytes per sample (int16)
                chunk = audio_buffer[:window_size * 2]
                audio_buffer = audio_buffer[window_size * 2:]

                # Convert to float tensor
                audio_tensor = torch.frombuffer(bytes(chunk), dtype=torch.int16).float() / 32768.0

                # Run Silero VAD
                speech_prob = model(audio_tensor, vad_sr).item()

                if speech_prob > 0.5:
                    # Speech detected
                    if not speech_detected:
                        speech_detected = True
                        silence_start = None
                        speculative_sent = False
                        await ws.send_json({"event": "speech_start"})
                    else:
                        silence_start = None
                        speculative_sent = False
                elif speech_detected:
                    # Silence after speech
                    now = time.time()
                    if silence_start is None:
                        silence_start = now

                    silence_duration = now - silence_start

                    # 1.0s silence → speculative STT in background
                    if silence_duration >= 1.0 and not speculative_sent:
                        speculative_sent = True
                        await ws.send_json({"event": "silence_1s"})
                        # Send audio to STT in background
                        audio_copy = bytes(all_audio)
                        def run_speculative_stt(audio_data):
                            text, ms = transcribe_audio(audio_data, "webm")
                            import asyncio
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    ws.send_json({"event": "stt_result", "transcript": text, "stt_ms": ms, "speculative": True}),
                                    asyncio.get_event_loop()
                                )
                            except: pass
                        threading.Thread(target=run_speculative_stt, args=(audio_copy,), daemon=True).start()

                    # 2.0s silence → final, stop
                    if silence_duration >= 2.0:
                        await ws.send_json({"event": "silence_final"})
                        speech_detected = False
                        silence_start = None

    except WebSocketDisconnect:
        print(f"[WS] Audio stream disconnected (session={sid})")
    except Exception as e:
        print(f"[WS] Error: {e}")


# ── TTS ──────────────────────────────────────────────────────────────────

def synthesize_speech(text: str) -> tuple[str, int]:
    """Returns (base64_audio, latency_ms)."""
    if not RUNTIME_CONFIG.get("tts_enabled", True) or not text:
        return "", 0
    provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
    voice = RUNTIME_CONFIG.get("tts_voice", "aura-asteria-en")
    t0 = time.time()

    # Deepgram
    if provider == "deepgram" and DEEPGRAM_API_KEY:
        try:
            r = http_requests.post(f"https://api.deepgram.com/v1/speak?model={voice}",
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000]}, timeout=15)
            r.raise_for_status()
            latency = round((time.time() - t0) * 1000)
            print(f"[TTS] Deepgram {latency}ms — {len(text)} chars")
            return base64.b64encode(r.content).decode(), latency
        except Exception as e:
            print(f"[TTS] Deepgram error: {e}")

    # Inworld
    if provider == "inworld" and INWORLD_API_KEY:
        try:
            r = http_requests.post("https://api.inworld.ai/tts/v1/voice",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000], "voiceId": voice or INWORLD_VOICE_ID, "modelId": INWORLD_MODEL_ID}, timeout=15)
            r.raise_for_status()
            latency = round((time.time() - t0) * 1000)
            print(f"[TTS] Inworld {latency}ms — {len(text)} chars")
            data = r.json() if "json" in r.headers.get("content-type", "") else None
            if data: return data.get("audioContent", base64.b64encode(r.content).decode()), latency
            return base64.b64encode(r.content).decode(), latency
        except Exception as e:
            print(f"[TTS] Inworld error: {e}")

    # OpenAI TTS
    if OPENAI_API_KEY:
        try:
            response = openai_client.audio.speech.create(model="tts-1", voice=voice or "nova", input=text[:2000])
            latency = round((time.time() - t0) * 1000)
            print(f"[TTS] OpenAI {latency}ms — {len(text)} chars")
            return base64.b64encode(response.content).decode(), latency
        except Exception as e:
            print(f"[TTS] OpenAI error: {e}")

    return "", round((time.time() - t0) * 1000)


# ── Dynamic Interview Prompts (loaded from files) ───────────────────────

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

def _load_prompt(level: str, domain: str) -> str:
    """Load prompt from file. Falls back to trained_fresher if file not found."""
    # fresh_graduate uses trained_fresher prompts
    if level == "fresh_graduate":
        level = "trained_fresher"
    filename = f"{level}_{domain}.md"
    filepath = os.path.join(_PROMPTS_DIR, filename)
    try:
        with open(filepath, "r") as f:
            prompt = f.read()
        print(f"[Prompt] Loaded {filename}")
        return prompt
    except FileNotFoundError:
        print(f"[Prompt] {filename} not found, falling back to trained_fresher_physical_design.md")
        fallback = os.path.join(_PROMPTS_DIR, "trained_fresher_physical_design.md")
        with open(fallback, "r") as f:
            return f.read()

# Cache loaded prompts in memory
_PROMPT_CACHE = {}

def get_interview_prompt(level: str, domain: str) -> str:
    """Get prompt from cache or load from file."""
    key = (level, domain)
    if key not in _PROMPT_CACHE:
        _PROMPT_CACHE[key] = _load_prompt(level, domain)
    return _PROMPT_CACHE[key]

# Keep _BASE for backward compatibility (admin prompt viewer, playground)
_BASE = _load_prompt("trained_fresher", "physical_design")


def build_interview_prompt(session):
    """Build prompt by loading the right file for level + domain, then appending candidate info."""
    resume = session.get("resume", {})
    history = session.get("conversation", [])

    name = resume.get("candidate_name", "Candidate")
    level = resume.get("level", "trained_fresher")
    domain = resume.get("domain", "physical_design")
    years = resume.get("years_experience", 0)
    tools = ", ".join(str(t) for t in resume.get("tools", [])[:5]) or "not specified"
    projects = ", ".join(str(p) for p in resume.get("key_projects", [])[:3]) or "not specified"
    skills = ", ".join(str(s) for s in resume.get("skills", [])[:8]) or "not specified"

    # Load self-contained prompt for this level + domain
    base_prompt = get_interview_prompt(level, domain)
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

    system = base_prompt + candidate_info + returning_block

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


# ── Candidate Behavior Guard ───────────────────────────────────────────

def classify_candidate_answer(answer: str) -> str:
    """Classify candidate answer as normal, personal_question, or abusive.
    Uses LLM to detect across all languages."""
    prompt = f"""Classify this candidate's message in an interview context.
Return ONLY one word: normal, personal_question, or abusive

Rules:
- personal_question: candidate asks personal things about the interviewer (age, marital status, location, appearance, personal life, etc.)
- abusive: candidate uses abusive, offensive, insulting, or scolding language in ANY language (English, Hindi, Telugu, Tamil, etc.)
- normal: everything else (technical answers, "I don't know", greetings, etc.)

Candidate said: "{answer}"

Classification:"""
    try:
        result, _usage = call_llm([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=10)
        result = result.strip().lower().replace(".", "")
        if result in ("personal_question", "abusive"):
            return result
    except Exception as e:
        print(f"[Guard] Classification failed: {e}")
    return "normal"


def send_abuse_email(session, answer: str):
    """Send email to admin reporting abusive candidate behavior."""
    if not SMTP_USER or not SMTP_PASS or not ADMIN_EMAIL:
        print("[Guard] SMTP not configured — skipping abuse email")
        return

    resume = session.get("resume", {})
    candidate_name = resume.get("candidate_name", "Unknown")
    candidate_email = resume.get("email", "Not provided")
    domain = resume.get("domain", "Not specified")
    level = resume.get("level", "Not specified")
    session_id = session.get("id", "Unknown")
    turn = session.get("turn", 0)

    subject = f"[ALERT] Abusive Candidate — {candidate_name} — Session {session_id[:8]}"
    body = f"""ABUSIVE BEHAVIOR REPORTED — Interview Terminated

Candidate Details:
  Name:    {candidate_name}
  Email:   {candidate_email}
  Domain:  {domain}
  Level:   {level}

Session:
  ID:      {session_id}
  Turn:    {turn}
  Time:    {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

Abusive Message:
  "{answer}"

Action Taken:
  Interview was immediately terminated.
  Candidate was warned that a complaint has been raised.

---
VLSI Interview Agent (Automated Alert)
"""

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = ADMIN_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())
        print(f"[Guard] Abuse email sent to {ADMIN_EMAIL} for candidate {candidate_name}")
    except Exception as e:
        print(f"[Guard] Failed to send abuse email: {e}")


# ── AI Answer Detection ────────────────────────────────────────────────

def detect_ai_answer(answer: str, session: dict, turn_index: int):
    """Check if candidate answer is AI-generated. Runs in background thread.
    Uses Sapling API if available, falls back to LLM-based detection."""
    if not ANTICHEAT_FEATURES.get("ai_answer_detect", {}).get("enabled", True):
        return
    if not answer or len(answer.split()) < 10:
        return  # Too short to detect

    result = {"checked": True, "is_ai": False, "score": 0.0, "method": "", "sapling": None, "llm": None}

    # Method 1: Sapling API (if available)
    if SAPLING_API_KEY:
        try:
            r = http_requests.post("https://api.sapling.ai/api/v1/aidetect",
                json={"key": SAPLING_API_KEY, "text": answer[:2000]}, timeout=10)
            if r.ok:
                data = r.json()
                sap_score = data.get("score", 0)
                result["sapling"] = {"score": sap_score, "sentence_scores": data.get("sentence_scores", [])}
                result["method"] = "sapling"
                if sap_score > 0.7:
                    result["is_ai"] = True
                    result["score"] = sap_score
                    print(f"[AI Detect] Sapling: {sap_score:.2f} — AI detected (turn {turn_index})")
                else:
                    print(f"[AI Detect] Sapling: {sap_score:.2f} — Human (turn {turn_index})")
        except Exception as e:
            print(f"[AI Detect] Sapling failed: {e}")

    # Method 2: LLM-based detection (fallback or cross-check)
    if not SAPLING_API_KEY or (result["sapling"] and result["sapling"]["score"] > 0.4):
        try:
            prompt = f"""Analyze if this interview answer was written by AI or a human.
Return ONLY valid JSON: {{"is_ai": true/false, "confidence": 0.0-1.0, "signals": ["signal1"]}}

Signs of AI-generated text:
- Unnaturally structured (intro, body, conclusion for a verbal answer)
- Perfect grammar and punctuation in a spoken interview
- Generic phrases like "It's important to note", "In summary", "There are several key aspects"
- Lists with parallel structure (a spoken answer would be messier)
- No filler words, hesitation, or personal experience references
- Covers too many points perfectly for a timed verbal response

Signs of human answer:
- Conversational tone, incomplete sentences
- Personal references ("In my project...", "I used to...")
- Filler words, self-corrections
- Focused on 1-2 points rather than comprehensive coverage
- Domain-specific jargon used naturally (not textbook definitions)

ANSWER: "{answer[:1500]}"

JSON:"""
            t0_ai = time.time()
            raw, ai_usage = call_llm([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=150)
            ai_ms = round((time.time() - t0_ai) * 1000)
            parsed = safe_json(raw)
            if parsed:
                llm_is_ai = parsed.get("is_ai", False)
                llm_conf = parsed.get("confidence", 0)
                result["llm"] = {"is_ai": llm_is_ai, "confidence": llm_conf, "signals": parsed.get("signals", [])}
                if not result["sapling"]:
                    result["method"] = "llm"
                    result["is_ai"] = llm_is_ai and llm_conf > 0.7
                    result["score"] = llm_conf
                else:
                    result["method"] = "sapling+llm"
                    # Both agree = high confidence
                    if result["sapling"]["score"] > 0.5 and llm_is_ai:
                        result["is_ai"] = True
                        result["score"] = max(result["sapling"]["score"], llm_conf)
                print(f"[AI Detect] LLM: is_ai={llm_is_ai} conf={llm_conf:.2f} (turn {turn_index}) | ${ai_usage['cost_usd']:.4f}")
            # Track AI detection cost
            session.setdefault("obs_log", []).append(
                _obs_entry("LLM_ai_detect", RUNTIME_CONFIG["qgen_model"], ai_ms, "success",
                           input_tokens=ai_usage["input_tokens"], output_tokens=ai_usage["output_tokens"],
                           cost_usd=ai_usage["cost_usd"]))
        except Exception as e:
            print(f"[AI Detect] LLM detection failed: {e}")

    # Store result in conversation entry
    if turn_index < len(session.get("conversation", [])):
        session["conversation"][turn_index]["ai_detection"] = result

    if result["is_ai"]:
        print(f"[AI Detect] WARNING: AI-generated answer detected at turn {turn_index} (score={result['score']:.2f}, method={result['method']})")


def generate_question(session, candidate_answer: str) -> dict:
    """Send conversation + answer to LLM, get next question. LLM handles all intelligence."""

    # Add candidate's answer to history
    if session["conversation"]:
        session["conversation"][-1]["answer"] = candidate_answer
        # Run AI detection in background (non-blocking)
        turn_idx = len(session["conversation"]) - 1
        threading.Thread(target=detect_ai_answer, args=(candidate_answer, session, turn_idx), daemon=True).start()

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

    # Single LLM call — handles question generation + behavior detection
    t0_llm = time.time()
    question, usage = call_llm(messages, temperature=0.7, max_tokens=120)
    llm_ms = round((time.time() - t0_llm) * 1000)
    print(f"[LLM] {RUNTIME_CONFIG['qgen_model']} {llm_ms}ms — turn {session['turn']} | in={usage['input_tokens']} out={usage['output_tokens']} ${usage['cost_usd']:.4f}")

    # Clean markdown
    question = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', question)
    question = re.sub(r'`([^`]+)`', r'\1', question)
    question = re.sub(r'#{1,3}\s*', '', question)

    obs = _obs_entry("LLM_question", RUNTIME_CONFIG["qgen_model"], llm_ms, "success",
                     input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                     cost_usd=usage["cost_usd"])

    # Check behavior tags from LLM
    if "[PERSONAL]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
        reply = question.replace("[PERSONAL]", "").strip()
        session["conversation"].append({"question": reply, "answer": None, "turn": session["turn"]})
        session["turn"] += 1
        session.setdefault("obs_log", []).append(obs)
        return {"question": reply, "should_end": False, "llm_ms": llm_ms}

    if "[ABUSIVE]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
        reply = question.replace("[ABUSIVE]", "").strip()
        session["phase"] = "ended"
        session["conversation"].append({"question": reply, "answer": None, "turn": session["turn"]})
        session.setdefault("obs_log", []).append(obs)
        if ANTICHEAT_FEATURES.get("abuse_email_alert", {}).get("enabled", True):
            threading.Thread(target=send_abuse_email, args=(session, candidate_answer), daemon=True).start()
        return {"question": reply, "should_end": True, "llm_ms": llm_ms}

    # Check if LLM decided to end the interview
    llm_end = "[END_INTERVIEW]" in question
    if llm_end:
        question = question.replace("[END_INTERVIEW]", "").strip()
        session["phase"] = "ended"

    # Add to history
    session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
    session["turn"] += 1

    # Store LLM timing + cost
    session.setdefault("obs_log", []).append(obs)

    return {"question": question, "should_end": llm_end, "llm_ms": llm_ms}


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

    # Get previous greetings to avoid repetition
    prev_greetings = []
    for ps in prev_sessions[-2:]:
        qs = ps.get("questions_asked", [])
        if qs:
            prev_greetings.append(qs[0])

    no_repeat = ""
    if prev_greetings:
        no_repeat = "\n\nDo NOT repeat or rephrase these previous greetings:\n" + "\n".join(f'- "{g}"' for g in prev_greetings) + "\nSay something COMPLETELY different this time."

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
- Sound like a real person, not a script{no_repeat}"""

    try:
        t0_greet = time.time()
        greeting, greet_usage = call_llm([{"role": "user", "content": context}], temperature=0.8, max_tokens=60)
        greet_ms = round((time.time() - t0_greet) * 1000)
        greeting = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', greeting).strip()
        session.setdefault("obs_log", []).append(
            _obs_entry("LLM_greeting", RUNTIME_CONFIG["qgen_model"], greet_ms, "success",
                       input_tokens=greet_usage["input_tokens"], output_tokens=greet_usage["output_tokens"],
                       cost_usd=greet_usage["cost_usd"]))
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
    return open("templates/index.html", encoding="utf-8").read()

@app.get("/interview", response_class=HTMLResponse)
async def interview_page():
    return open("templates/voice_agent_ui.html", encoding="utf-8").read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    return open("templates/admin.html", encoding="utf-8").read()

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
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content); tmp_path = tmp.name
            text = ""
            # Try PyMuPDF first (handles both text and scanned PDFs)
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(tmp_path)
                for page in doc:
                    text += (page.get_text() or "") + "\n"
                doc.close()
                print(f"[Resume] PyMuPDF extracted {len(text.strip())} chars")
            except ImportError:
                pass
            # Fallback to PyPDF2 if PyMuPDF not available or extracted nothing
            if not text.strip():
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(tmp_path)
                    for page in reader.pages:
                        text += (page.extract_text() or "") + "\n"
                    print(f"[Resume] PyPDF extracted {len(text.strip())} chars")
                except ImportError:
                    pass
            # Fallback to pdfplumber
            if not text.strip():
                try:
                    import pdfplumber
                    with pdfplumber.open(tmp_path) as pdf:
                        for page in pdf.pages:
                            text += (page.extract_text() or "") + "\n"
                    print(f"[Resume] pdfplumber extracted {len(text.strip())} chars")
                except ImportError:
                    pass
        except Exception as e:
            raise HTTPException(400, f"PDF error: {e}")
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except: pass
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
    audio, tts_ms = synthesize_speech(greeting)
    session["phase"] = "interview"

    # Track greeting TTS cost
    tts_provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
    session.setdefault("obs_log", []).append(
        _obs_entry("TTS_greeting", tts_provider, tts_ms, "success" if audio else "failure",
                   chars=len(greeting), cost_usd=_calc_tts_cost(tts_provider, len(greeting))))

    return {
        "question": greeting, "question_type": "greeting", "turn": session["turn"],
        "phase": session["phase"], "audio": audio, "difficulty": "basic",
        "should_end": False, "resume": session.get("resume", {}),
        "timing": {"tts_ms": tts_ms},
    }


@app.post("/api/transcribe")
async def transcribe_endpoint(audio: UploadFile = File(...), session_id: str = Form("")):
    audio_bytes = await audio.read()
    ext = audio.filename.rsplit(".", 1)[-1] if audio.filename else "webm"
    transcript, stt_ms = transcribe_audio(audio_bytes, ext)
    # Store STT timing + cost in session
    stt_model = RUNTIME_CONFIG.get("stt_model", "gpt-4o-mini-transcribe")
    session = sessions.get(session_id)
    if session:
        session.setdefault("obs_log", []).append(
            _obs_entry("STT", stt_model, stt_ms, "success" if transcript else "failure",
                       chars=len(transcript), cost_usd=_calc_stt_cost(stt_model, stt_ms)))
    return {"transcript": transcript, "stt_ms": stt_ms}


@app.post("/api/submit-answer")
async def submit_answer(data: dict):
    sid = data.get("session_id")
    answer = data.get("answer", "")
    session = sessions.get(sid)
    if not session: raise HTTPException(404, "Session not found")

    t0_total = time.time()
    result = generate_question(session, answer)
    audio, tts_ms = synthesize_speech(result["question"])

    # Store TTS timing + cost
    tts_provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
    session.setdefault("obs_log", []).append(
        _obs_entry("TTS", tts_provider, tts_ms, "success" if audio else "failure",
                   chars=len(result["question"]), cost_usd=_calc_tts_cost(tts_provider, len(result["question"]))))

    if result["should_end"]:
        session["phase"] = "ended"
        save_candidate_session(session)

    total_ms = round((time.time() - t0_total) * 1000)
    llm_ms = result.get("llm_ms", 0)
    print(f"[Turn {session['turn']}] Total: {total_ms}ms (LLM: {llm_ms}ms + TTS: {tts_ms}ms)")

    return {
        "question": result["question"], "question_type": "interview",
        "turn": session["turn"], "phase": session["phase"],
        "audio": audio, "difficulty": "basic",
        "should_end": result["should_end"],
        "timing": {"llm_ms": llm_ms, "tts_ms": tts_ms, "total_ms": total_ms},
    }


@app.post("/api/stream-answer")
async def stream_answer(data: dict):
    """SSE endpoint: LLM streams tokens → sentence buffer → TTS per sentence → audio chunks to client."""
    sid = data.get("session_id")
    answer = data.get("answer", "")
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")

    def event_stream():
        t0 = time.time()

        # Add candidate's answer to history
        if session["conversation"]:
            session["conversation"][-1]["answer"] = answer
            turn_idx = len(session["conversation"]) - 1
            threading.Thread(target=detect_ai_answer, args=(answer, session, turn_idx), daemon=True).start()

        # Check auto-end
        should_end, end_msg = _should_end_interview(session)
        if should_end:
            session["phase"] = "ended"
            audio_bytes = tts_chunk(end_msg)
            yield f"data: {json.dumps({'type': 'text', 'content': end_msg, 'done': True, 'should_end': True})}\n\n"
            if audio_bytes:
                yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode()})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'turn': session['turn'], 'phase': 'ended'})}\n\n"
            return

        # Build prompt
        messages = build_interview_prompt(session)
        phase = _get_interview_phase(session["turn"])
        topics_covered = _get_topics_covered(session)
        pacing = f"\nPHASE: {phase} | Turn: {session['turn']}"
        if topics_covered:
            pacing += f"\nTopics covered: {', '.join(topics_covered)}. Ask about DIFFERENT topics."
        messages.append({"role": "user", "content": answer + pacing})

        # Stream LLM tokens, buffer into sentences
        t0_llm = time.time()
        full_text = ""
        sentence_buffer = ""
        sentence_count = 0
        total_tts_ms = 0
        total_tts_chars = 0
        input_tokens_est = _estimate_message_tokens(messages)

        for token in stream_llm(messages, temperature=0.7, max_tokens=120):
            full_text += token
            sentence_buffer += token

            # Send text token to frontend immediately (for typewriter)
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            # Check for sentence boundary
            if re.search(r'[.?!]\s*$', sentence_buffer) or len(sentence_buffer) > 150:
                sentence = sentence_buffer.strip()
                sentence_buffer = ""
                if sentence:
                    sentence_count += 1
                    # Generate TTS for this sentence
                    t0_tts = time.time()
                    audio_bytes = tts_chunk(sentence)
                    tts_ms = round((time.time() - t0_tts) * 1000)
                    total_tts_ms += tts_ms
                    total_tts_chars += len(sentence)
                    if audio_bytes:
                        yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode(), 'tts_ms': tts_ms})}\n\n"
                    print(f"[Stream] Sentence {sentence_count}: TTS {tts_ms}ms — \"{sentence[:50]}...\"")

        # Flush remaining buffer
        if sentence_buffer.strip():
            sentence = sentence_buffer.strip()
            t0_tts = time.time()
            audio_bytes = tts_chunk(sentence)
            tts_ms = round((time.time() - t0_tts) * 1000)
            total_tts_ms += tts_ms
            total_tts_chars += len(sentence)
            if audio_bytes:
                yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode(), 'tts_ms': tts_ms})}\n\n"

        llm_ms = round((time.time() - t0_llm) * 1000)
        output_tokens_est = _estimate_tokens(full_text)
        llm_cost = _calc_llm_cost(RUNTIME_CONFIG["qgen_model"], input_tokens_est, output_tokens_est)

        # Clean the full text
        question = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', full_text)
        question = re.sub(r'`([^`]+)`', r'\1', question)
        question = re.sub(r'#{1,3}\s*', '', question).strip()

        # Check behavior tags
        is_end = False
        if "[PERSONAL]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
            question = question.replace("[PERSONAL]", "").strip()
        elif "[ABUSIVE]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
            question = question.replace("[ABUSIVE]", "").strip()
            session["phase"] = "ended"
            is_end = True
            if ANTICHEAT_FEATURES.get("abuse_email_alert", {}).get("enabled", True):
                threading.Thread(target=send_abuse_email, args=(session, answer), daemon=True).start()
        elif "[END_INTERVIEW]" in question:
            question = question.replace("[END_INTERVIEW]", "").strip()
            session["phase"] = "ended"
            is_end = True

        # Update session
        session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
        session["turn"] += 1

        # Track LLM cost
        tts_provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
        tts_cost = _calc_tts_cost(tts_provider, total_tts_chars)
        session.setdefault("obs_log", []).append(
            _obs_entry("LLM_question", RUNTIME_CONFIG["qgen_model"], llm_ms, "success",
                       input_tokens=input_tokens_est, output_tokens=output_tokens_est, cost_usd=llm_cost))
        # Track TTS cost
        session["obs_log"].append(
            _obs_entry("TTS", tts_provider, total_tts_ms, "success",
                       chars=total_tts_chars, cost_usd=tts_cost))

        if is_end:
            save_candidate_session(session)

        total_ms = round((time.time() - t0) * 1000)
        print(f"[Stream Turn {session['turn']}] Total: {total_ms}ms (LLM: {llm_ms}ms, {sentence_count} TTS chunks)")

        # Final done event
        yield f"data: {json.dumps({'type': 'done', 'question': question, 'turn': session['turn'], 'phase': session['phase'], 'should_end': is_end, 'timing': {'llm_ms': llm_ms, 'total_ms': total_ms}})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


# ── Anti-cheat Config ──────────────────────────────────────────────────

ANTICHEAT_FEATURES = {
    "behavior_guard": {
        "label": "Behavior Guard",
        "description": "Detects personal questions and abusive language from candidates using LLM classification",
        "category": "behavioral",
        "enabled": True,
    },
    "abuse_email_alert": {
        "label": "Abuse Email Alert",
        "description": "Sends email to admin when abusive language is detected and interview is terminated",
        "category": "behavioral",
        "enabled": True,
    },
    "tab_switch": {
        "label": "Tab Switch Detection",
        "description": "Logs when candidate switches browser tabs during the interview",
        "category": "browser",
        "enabled": True,
    },
    "window_blur": {
        "label": "Window Blur Detection",
        "description": "Logs when the interview window loses focus",
        "category": "browser",
        "enabled": True,
    },
    "paste_detect": {
        "label": "Paste Detection",
        "description": "Logs when candidate pastes text into the answer field",
        "category": "browser",
        "enabled": True,
    },
    "screen_share": {
        "label": "Screen Share Detection",
        "description": "Detects if candidate starts screen sharing during the interview",
        "category": "browser",
        "enabled": True,
    },
    "dom_overlay": {
        "label": "AI Extension Detection",
        "description": "Detects high-z-index overlays from AI browser extensions (copilots, assistants)",
        "category": "ai_detect",
        "enabled": True,
    },
    "canary_trigger": {
        "label": "Canary Element Monitor",
        "description": "Hidden DOM element that detects if AI tools read or modify page content",
        "category": "ai_detect",
        "enabled": True,
    },
    "ai_answer_detect": {
        "label": "AI Answer Detection",
        "description": "Checks if candidate answers are AI-generated using Sapling API and LLM analysis",
        "category": "ai_detect",
        "enabled": True,
    },
    "phone_detect": {
        "label": "Mobile Phone Detection",
        "description": "Uses COCO-SSD model to detect mobile phones in webcam feed during interview",
        "category": "camera",
        "enabled": True,
    },
    "face_detect": {
        "label": "Face Detection",
        "description": "Verify candidate face is visible on camera throughout the interview",
        "category": "camera",
        "enabled": False,
    },
"eye_away": {
        "label": "Eye Tracking",
        "description": "Track if candidate is reading from another screen or notes",
        "category": "camera",
        "enabled": False,
    },
}

@app.post("/api/anticheat-event")
async def anticheat_event(data: dict):
    event_type = data.get("event_type", "")
    # Check if this event type's feature is enabled
    feature_map = {
        "tab_switch": "tab_switch", "window_blur": "window_blur",
        "paste_event": "paste_detect", "screen_share": "screen_share",
        "dom_overlay": "dom_overlay", "canary_triggered": "canary_trigger",
    }
    feature_key = feature_map.get(event_type)
    if feature_key and not ANTICHEAT_FEATURES.get(feature_key, {}).get("enabled", True):
        return {"ok": True, "ignored": True}
    # Log the event
    sid = data.get("session_id", "")
    session = sessions.get(sid)
    if session:
        session.setdefault("anticheat_log", []).append({
            "event_type": event_type,
            "turn": data.get("turn", 0),
            "timestamp": data.get("timestamp", time.time()),
            "metadata": data.get("metadata", ""),
        })
    return {"ok": True}

@app.get("/api/anticheat-settings")
async def anticheat_settings():
    return {k: v["enabled"] for k, v in ANTICHEAT_FEATURES.items()}

@app.post("/api/sim/ai-done")
async def sim_ai_done(data: dict):
    return {"ok": True}


# ── Admin: LLM Config ───────────────────────────────────────────────────

AVAILABLE_MODELS = [
    # Fast tier
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "tier": "fast", "input_cost": "$0.15/1M", "output_cost": "$0.60/1M", "latency": "~1-2s", "context": "128K", "best_for": "Fast, cheap"},
    {"id": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "name": "Claude Haiku 4.5", "tier": "fast", "input_cost": "$1.00/1M", "output_cost": "$5.00/1M", "latency": "~0.5-1s", "context": "200K", "best_for": "Question generation"},
    {"id": "grok-4-1-fast-non-reasoning", "name": "Grok 4.1 Fast", "tier": "fast", "input_cost": "$0.20/1M", "output_cost": "$0.50/1M", "latency": "~0.3-0.5s", "context": "2M", "best_for": "Fastest response"},
    {"id": "us.meta.llama4-maverick-17b-instruct-v1:0", "name": "Llama 4 Maverick 17B", "tier": "fast", "input_cost": "$0.17/1M", "output_cost": "$0.17/1M", "latency": "~0.5-1s", "context": "128K", "best_for": "Cheapest"},
    {"id": "us.meta.llama4-scout-17b-instruct-v1:0", "name": "Llama 4 Scout 17B", "tier": "fast", "input_cost": "$0.17/1M", "output_cost": "$0.17/1M", "latency": "~0.5-1s", "context": "128K", "best_for": "Fast, cheap"},
    {"id": "us.amazon.nova-lite-v1:0", "name": "Amazon Nova Lite", "tier": "fast", "input_cost": "$0.06/1M", "output_cost": "$0.24/1M", "latency": "~0.5s", "context": "300K", "best_for": "AWS native, cheapest"},
    {"id": "us.amazon.nova-micro-v1:0", "name": "Amazon Nova Micro", "tier": "fast", "input_cost": "$0.035/1M", "output_cost": "$0.14/1M", "latency": "~0.3s", "context": "128K", "best_for": "Ultra-cheap"},
    # Balanced tier
    {"id": "us.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "tier": "balanced", "input_cost": "$3.00/1M", "output_cost": "$15.00/1M", "latency": "~1-2s", "context": "200K", "best_for": "Evaluation, best balance"},
    {"id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "name": "Claude Sonnet 4.5", "tier": "balanced", "input_cost": "$3.00/1M", "output_cost": "$15.00/1M", "latency": "~1-2s", "context": "200K", "best_for": "Evaluation"},
    {"id": "grok-4-1-fast-reasoning", "name": "Grok 4.1 Fast Reasoning", "tier": "balanced", "input_cost": "$0.20/1M", "output_cost": "$0.50/1M", "latency": "~1-2s", "context": "2M", "best_for": "Reasoning tasks"},
    {"id": "us.amazon.nova-pro-v1:0", "name": "Amazon Nova Pro", "tier": "balanced", "input_cost": "$0.80/1M", "output_cost": "$3.20/1M", "latency": "~1-2s", "context": "300K", "best_for": "AWS balanced"},
    {"id": "us.meta.llama3-3-70b-instruct-v1:0", "name": "Llama 3.3 70B", "tier": "balanced", "input_cost": "$0.72/1M", "output_cost": "$0.72/1M", "latency": "~1-2s", "context": "128K", "best_for": "Open source, strong"},
    {"id": "us.deepseek.r1-v1:0", "name": "DeepSeek R1", "tier": "balanced", "input_cost": "$1.35/1M", "output_cost": "$5.40/1M", "latency": "~2-4s", "context": "128K", "best_for": "Deep reasoning"},
    {"id": "us.mistral.pixtral-large-2502-v1:0", "name": "Mistral Pixtral Large", "tier": "balanced", "input_cost": "$2.00/1M", "output_cost": "$6.00/1M", "latency": "~1-2s", "context": "128K", "best_for": "Multimodal"},
    # Premium tier
    {"id": "us.anthropic.claude-opus-4-6-v1", "name": "Claude Opus 4.6", "tier": "premium", "input_cost": "$15.00/1M", "output_cost": "$75.00/1M", "latency": "~3-5s", "context": "200K", "best_for": "Highest accuracy"},
    {"id": "us.anthropic.claude-opus-4-5-20251101-v1:0", "name": "Claude Opus 4.5", "tier": "premium", "input_cost": "$15.00/1M", "output_cost": "$75.00/1M", "latency": "~3-5s", "context": "200K", "best_for": "Premium evaluation"},
    {"id": "us.amazon.nova-premier-v1:0", "name": "Amazon Nova Premier", "tier": "premium", "input_cost": "$2.50/1M", "output_cost": "$10.00/1M", "latency": "~2-4s", "context": "300K", "best_for": "AWS premium"},
    {"id": "us.writer.palmyra-x5-v1:0", "name": "Writer Palmyra X5", "tier": "premium", "input_cost": "$5.00/1M", "output_cost": "$25.00/1M", "latency": "~2-3s", "context": "128K", "best_for": "Enterprise writing"},
]

@app.get("/api/admin/llm-config")
async def get_llm_config(_=Depends(require_admin)):
    return {"qgen_model": RUNTIME_CONFIG["qgen_model"], "eval_model": RUNTIME_CONFIG["eval_model"], "available_models": AVAILABLE_MODELS}

@app.post("/api/admin/llm-config")
async def set_llm_config(data: dict, _=Depends(require_admin)):
    if "qgen_model" in data: RUNTIME_CONFIG["qgen_model"] = data["qgen_model"]
    if "eval_model" in data: RUNTIME_CONFIG["eval_model"] = data["eval_model"]
    return {"status": "success", "qgen_model": RUNTIME_CONFIG["qgen_model"], "eval_model": RUNTIME_CONFIG["eval_model"]}

EDITABLE_PROMPTS = {
    "qgen_rules": """- Ask ONE question at a time. Max 2 sentences.
- React to the candidate's answer before asking the next question.
- Start with basic questions, increase difficulty if they answer well.
- Be conversational and natural, like a real interview.
- If they don't know, give a small hint and move on.
- Never teach or explain. Just ask and react.""",
    "eval_prompt": """You are a senior VLSI interview evaluator. Score this candidate's answer.

CANDIDATE: {domain} | {level} | {years} years experience
QUESTION ({question_type}, {difficulty}): {question}
ANSWER: {answer}

Score 0-10 and return ONLY valid JSON:
{{"score": 0, "quality": "strong|adequate|weak|honest_admission", "accuracy": "correct|partial|incorrect|not_applicable",
"quadrant": "genuine_expert|genuine_nervous|honest_confused|dangerous_fake",
"score_reasoning": "1-2 sentence explanation",
"expected_points": ["point1", "point2"],
"missing_points": ["missed1"],
"level_gap": 0,
"notes": "brief evaluator note"}}

Rules:
- Score relative to candidate's level (fresh grad scored differently than senior)
- "I don't know" = honest_admission, score 5-6 (shows integrity)
- Textbook-perfect answers from freshers = suspicious, note it
- Partial but genuine answers score higher than memorized-sounding complete ones
- 0-3: wrong/no answer, 4-5: partial, 6-7: adequate, 8-9: strong, 10: exceptional""",
}

@app.get("/api/admin/llm-prompts")
async def get_llm_prompts(_=Depends(require_admin)):
    return {"eval_prompt": EDITABLE_PROMPTS["eval_prompt"], "qgen_rules": EDITABLE_PROMPTS["qgen_rules"], "qgen_prompt": _BASE}

@app.post("/api/admin/llm-prompts")
async def set_llm_prompts(data: dict, _=Depends(require_admin)):
    if data.get("reset_eval"):
        EDITABLE_PROMPTS["eval_prompt"] = EDITABLE_PROMPTS["eval_prompt"]  # already default
    elif "eval_prompt" in data:
        EDITABLE_PROMPTS["eval_prompt"] = data["eval_prompt"]
    if data.get("reset_qgen"):
        EDITABLE_PROMPTS["qgen_rules"] = EDITABLE_PROMPTS["qgen_rules"]
    elif "qgen_rules" in data:
        EDITABLE_PROMPTS["qgen_rules"] = data["qgen_rules"]
    return {"status": "success"}

@app.get("/api/admin/qgen-prompt")
async def get_qgen_prompt(domain: str = "physical_design", level: str = "trained_fresher", name: str = "Sample", _=Depends(require_admin)):
    prompt = get_interview_prompt(level, domain)
    prompt += f"\nCANDIDATE: {name} | {level.replace('_',' ')} | Tools: not specified"
    return {"prompt": prompt}


# ── Admin: Voice Config ──────────────────────────────────────────────────

@app.post("/api/toggle-tts")
async def toggle_tts(data: dict):
    RUNTIME_CONFIG["tts_enabled"] = data.get("enabled", True)
    return {"ok": True, "tts_enabled": RUNTIME_CONFIG["tts_enabled"]}

@app.get("/api/tts-status")
async def tts_status():
    return {"tts_enabled": RUNTIME_CONFIG["tts_enabled"]}

@app.get("/api/admin/stt-config")
async def get_stt_config(_=Depends(require_admin)):
    return {
        "provider": RUNTIME_CONFIG["stt_provider"],
        "model": RUNTIME_CONFIG["stt_model"],
        "available": [
            {"provider": "openai", "model": "gpt-4o-mini-transcribe", "name": "OpenAI Whisper (gpt-4o-mini-transcribe)", "latency": "~500-1300ms", "cost": "$0.006/min"},
            {"provider": "openai", "model": "whisper-1", "name": "OpenAI Whisper-1", "latency": "~400-800ms", "cost": "$0.006/min"},
            {"provider": "deepgram", "model": "nova-3", "name": "Deepgram Nova-3 (fastest)", "latency": "~200-500ms", "cost": "$0.0059/min"},
            {"provider": "deepgram", "model": "nova-2", "name": "Deepgram Nova-2", "latency": "~300-600ms", "cost": "$0.0043/min"},
        ],
    }

@app.post("/api/admin/stt-config")
async def set_stt_config(data: dict, _=Depends(require_admin)):
    if "provider" in data:
        RUNTIME_CONFIG["stt_provider"] = data["provider"]
    if "model" in data:
        RUNTIME_CONFIG["stt_model"] = data["model"]
    print(f"[STT Config] Changed to {RUNTIME_CONFIG['stt_provider']}/{RUNTIME_CONFIG['stt_model']}")
    return {"status": "success", "provider": RUNTIME_CONFIG["stt_provider"], "model": RUNTIME_CONFIG["stt_model"]}

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
    audio, tts_ms = synthesize_speech(text)
    RUNTIME_CONFIG["tts_provider"], RUNTIME_CONFIG["tts_voice"] = old_p, old_v
    return {"audio": audio, "latency_ms": tts_ms}

@app.get("/api/admin/voice-library")
async def voice_library(_=Depends(require_admin)):
    return {"voices": []}

@app.post("/api/admin/clone-voice")
async def clone_voice(_=Depends(require_admin)):
    return {"ok": False, "error": "Not supported in simple mode"}

@app.post("/api/playground/tts")
async def playground_tts(data: dict):
    text = data.get("text", "")
    audio, tts_ms = synthesize_speech(text)
    return {"audio": audio, "tts_ms": tts_ms}


# ── Admin: Prompt Playground ─────────────────────────────────────────────

@app.post("/api/admin/prompt-playground")
async def prompt_playground(data: dict, _=Depends(require_admin)):
    prompt_text = data.get("prompt", "")
    messages = data.get("messages", [])
    model_id = data.get("model_id", RUNTIME_CONFIG["qgen_model"])
    temperature = data.get("temperature", 0.3)
    max_tokens = data.get("max_tokens", 600)
    system_prompt = data.get("system_prompt", "")

    msgs = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})

    # Multi-turn chat mode: frontend sends messages array
    if messages:
        msgs.extend(messages)
    elif prompt_text:
        msgs.append({"role": "user", "content": prompt_text})
    else:
        return {"status": "error", "error": "No prompt or messages provided", "latency_ms": 0, "model": model_id}

    t0 = time.time()
    try:
        raw, usage = call_llm(msgs, model_id=model_id, temperature=temperature, max_tokens=max_tokens)
        # Try to parse as JSON for eval prompts
        parsed = safe_json(raw)
        result = {"status": "success", "raw_response": raw, "latency_ms": round((time.time() - t0) * 1000), "model": model_id,
                  "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"], "cost_usd": usage["cost_usd"]}
        if parsed:
            result["parsed_json"] = parsed
        return result
    except Exception as e:
        return {"status": "error", "error": str(e), "latency_ms": round((time.time() - t0) * 1000), "model": model_id}


# ── Admin: Anti-cheat Config ────────────────────────────────────────────

@app.get("/api/admin/anticheat-config")
async def get_anticheat_config(_=Depends(require_admin)):
    return {"features": ANTICHEAT_FEATURES}

@app.post("/api/admin/anticheat-config")
async def set_anticheat_config(data: dict, _=Depends(require_admin)):
    for key, enabled in data.items():
        if key in ANTICHEAT_FEATURES and isinstance(enabled, bool):
            ANTICHEAT_FEATURES[key]["enabled"] = enabled
    return {"status": "success", "features": ANTICHEAT_FEATURES}


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


def _build_session_obs(sid, session):
    """Build observability summary for a single session with cost tracking."""
    logs = session.get("obs_log", [])
    total = len(logs)
    success = sum(1 for l in logs if l.get("status") == "success")
    latencies = [l["latency_ms"] for l in logs if l.get("latency_ms")]
    avg_lat = round(sum(latencies) / len(latencies)) if latencies else 0
    total_cost = sum(l.get("cost_usd", 0) for l in logs)
    total_input_tokens = sum(l.get("input_tokens", 0) for l in logs)
    total_output_tokens = sum(l.get("output_tokens", 0) for l in logs)
    by_step = {}
    for step in ["LLM_question", "LLM_greeting", "LLM_ai_detect", "STT", "TTS", "TTS_greeting"]:
        step_logs = [l for l in logs if l.get("step") == step]
        step_lats = [l["latency_ms"] for l in step_logs if l.get("latency_ms")]
        step_cost = sum(l.get("cost_usd", 0) for l in step_logs)
        step_in = sum(l.get("input_tokens", 0) for l in step_logs)
        step_out = sum(l.get("output_tokens", 0) for l in step_logs)
        step_chars = sum(l.get("chars", 0) for l in step_logs)
        if step_lats or step_cost > 0:
            by_step[step] = {
                "calls": len(step_lats),
                "avg_ms": round(sum(step_lats) / len(step_lats)) if step_lats else 0,
                "cost_usd": round(step_cost, 6),
                "input_tokens": step_in,
                "output_tokens": step_out,
                "chars": step_chars,
            }
    return {
        "session_id": sid, "total_calls": total, "success_calls": success,
        "failure_calls": total - success, "total_cost_usd": round(total_cost, 6),
        "total_input_tokens": total_input_tokens, "total_output_tokens": total_output_tokens,
        "avg_latency_ms": avg_lat, "step_breakdown": by_step,
    }

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
            "ai_detection": entry.get("ai_detection", {}),
        })
    return {
        "session_id": sid,
        "resume": session.get("resume", {}),
        "phase": session.get("phase", ""),
        "turn": session.get("turn", 0),
        "difficulty_level": session.get("difficulty_level", 1),
        "trajectory": "unknown",
        "turn_log": turn_log,
        "anticheat_log": session.get("anticheat_log", []),
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
        "observability": _build_session_obs(sid, session),
    }


# ── Admin: Observability ─────────────────────────────────────────────────

@app.get("/api/observability/summary")
async def obs_summary(window: int = 86400, _=Depends(require_admin)):
    cutoff = time.time() - window
    all_logs = []
    for s in sessions.values():
        if s.get("started_at", 0) >= cutoff:
            all_logs.extend(s.get("obs_log", []))

    total = len(all_logs)
    success = sum(1 for l in all_logs if l.get("status") == "success")
    failures = total - success
    latencies = [l["latency_ms"] for l in all_logs if l.get("latency_ms")]
    avg_lat = round(sum(latencies) / len(latencies)) if latencies else 0
    total_cost = sum(l.get("cost_usd", 0) for l in all_logs)
    total_input_tokens = sum(l.get("input_tokens", 0) for l in all_logs)
    total_output_tokens = sum(l.get("output_tokens", 0) for l in all_logs)

    # Step breakdown
    by_step = {}
    for step in ["LLM_question", "LLM_greeting", "LLM_ai_detect", "STT", "TTS", "TTS_greeting"]:
        step_logs = [l for l in all_logs if l.get("step") == step]
        step_lats = sorted([l["latency_ms"] for l in step_logs if l.get("latency_ms")])
        if step_lats:
            p50 = step_lats[len(step_lats) // 2]
            p95 = step_lats[min(len(step_lats) - 1, int(len(step_lats) * 0.95))]
            avg = round(sum(step_lats) / len(step_lats))
        else:
            p50 = p95 = avg = 0
        step_cost = sum(l.get("cost_usd", 0) for l in step_logs)
        step_in = sum(l.get("input_tokens", 0) for l in step_logs)
        step_out = sum(l.get("output_tokens", 0) for l in step_logs)
        step_chars = sum(l.get("chars", 0) for l in step_logs)
        if step_lats or step_cost > 0:
            by_step[step] = {
                "calls": len(step_logs),
                "failures": sum(1 for l in step_logs if l.get("status") != "success"),
                "p50": p50, "p95": p95, "avg": avg,
                "cost_usd": round(step_cost, 6),
                "input_tokens": step_in, "output_tokens": step_out, "chars": step_chars,
            }

    return {
        "total_calls": total, "success_calls": success, "failure_calls": failures,
        "total_cost_usd": round(total_cost, 6), "avg_latency_ms": avg_lat,
        "total_input_tokens": total_input_tokens, "total_output_tokens": total_output_tokens,
        "success_rate_pct": round(success / total * 100, 1) if total else 100,
        "by_step": by_step,
        "recent_errors": [],
    }

@app.get("/api/observability/logs")
async def obs_logs(limit: int = 500, _=Depends(require_admin)):
    all_logs = []
    for sid, s in sessions.items():
        for log in s.get("obs_log", []):
            all_logs.append({
                "ts_str": datetime.fromtimestamp(log.get("ts", s.get("started_at", 0))).strftime("%H:%M:%S"),
                "session_id": sid,
                "step": log.get("step", ""),
                "model": log.get("model", ""),
                "latency_ms": log.get("latency_ms"),
                "input_tokens": log.get("input_tokens", 0),
                "output_tokens": log.get("output_tokens", 0),
                "chars": log.get("chars", 0),
                "cost_usd": log.get("cost_usd", 0),
                "status": log.get("status", "success"),
                "error": log.get("error"),
            })
    return all_logs[-limit:]


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
