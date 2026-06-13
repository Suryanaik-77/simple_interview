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
from urllib.parse import quote
from secrets_proxy import get_secret

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
OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
DEEPGRAM_API_KEY = get_secret("DEEPGRAM_API_KEY")
INWORLD_API_KEY = get_secret("INWORLD_API_KEY")
INWORLD_VOICE_ID = get_secret("INWORLD_VOICE_ID", "Sarah")
INWORLD_MODEL_ID = get_secret("INWORLD_MODEL_ID", "inworld-tts-1.5-mini")
KUGEL_API_KEY = get_secret("KUGEL_API_KEY")
KUGEL_MODEL_ID = get_secret("KUGEL_MODEL_ID", "kugel-2.5")
KUGEL_SAMPLE_RATE = int(get_secret("KUGEL_SAMPLE_RATE", "24000"))
XAI_API_KEY = get_secret("XAI_API_KEY")
CEREBRAS_API_KEY = get_secret("CEREBRAS_API_KEY")
JWT_SECRET = get_secret("JWT_SECRET") or secrets.token_hex(32)
ADMIN_USER = get_secret("ADMIN_USER", "admin")
ADMIN_PASS = get_secret("ADMIN_PASS", "admin123")
SMTP_HOST = get_secret("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(get_secret("SMTP_PORT", "587"))
SMTP_USER = get_secret("SMTP_USER")
SMTP_PASS = get_secret("SMTP_PASS")
ADMIN_EMAIL = get_secret("ADMIN_EMAIL", "gsuryanaik7@gmail.com")
SAPLING_API_KEY = get_secret("SAPLING_API_KEY")
LMS_API_KEY = get_secret("LMS_API_KEY", "")            # shared secret for LMS → interview API
LMS_REDIRECT_URL = get_secret("LMS_REDIRECT_URL", "")  # redirect when user hits /interview without token

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
import database
database.init_db()

import redis_cache
redis_cache.init_cache()

# Shared runtime config (LLM/TTS/STT). With multiple workers, an admin edit only
# mutates one worker's RUNTIME_CONFIG dict. Postgres holds the durable, shared copy
# (so changes survive restarts and reach every worker); Redis caches it for fast reads.
_RUNTIME_CONFIG_KEY = "config:runtime"   # Redis key
_CONFIG_DB_KEY = "runtime"               # app_config.config_key


def _sync_runtime_config():
    """Refresh RUNTIME_CONFIG from the shared store so all workers agree.
    Redis first (fast); Postgres (durable) on miss, repopulating Redis. No-op if
    neither is available — the in-process dict stays the source of truth."""
    if redis_cache.is_available():
        cfg = redis_cache.get_json(_RUNTIME_CONFIG_KEY)
        if cfg:
            RUNTIME_CONFIG.update(cfg)
            return
    if database.is_available():
        cfg = database.get_app_config(_CONFIG_DB_KEY)
        if cfg:
            RUNTIME_CONFIG.update(cfg)
            if redis_cache.is_available():
                redis_cache.set_json(_RUNTIME_CONFIG_KEY, RUNTIME_CONFIG)


def _persist_runtime_config():
    """After an admin change: write Postgres (durable) and refresh the Redis cache,
    so the new config survives restarts and reaches the other workers."""
    if database.is_available():
        database.save_app_config(_CONFIG_DB_KEY, RUNTIME_CONFIG)
    if redis_cache.is_available():
        redis_cache.set_json(_RUNTIME_CONFIG_KEY, RUNTIME_CONFIG)


# Adopt persisted config at boot so an admin's earlier changes survive restarts;
# seed defaults on first run. Postgres is the source of truth, so only refresh
# Redis from it when we actually read/seeded the durable value. If Postgres is
# unavailable at this boot (e.g. a transient connection failure during a
# max-requests worker recycle), never overwrite the shared Redis value with this
# worker's hardcoded defaults — that would silently revert an admin's model
# choice for every worker. Seed Redis only when the key is absent.
_seeded_from_db = False
if database.is_available():
    _persisted_cfg = database.get_app_config(_CONFIG_DB_KEY)
    if _persisted_cfg:
        RUNTIME_CONFIG.update(_persisted_cfg)
    else:
        database.save_app_config(_CONFIG_DB_KEY, RUNTIME_CONFIG)
    _seeded_from_db = True
if redis_cache.is_available():
    if _seeded_from_db:
        redis_cache.set_json(_RUNTIME_CONFIG_KEY, RUNTIME_CONFIG)
    elif redis_cache.get_json(_RUNTIME_CONFIG_KEY) is None:
        redis_cache.set_json(_RUNTIME_CONFIG_KEY, RUNTIME_CONFIG)


class DatabaseSessions:
    def __init__(self):
        self._memory = {}

    def __contains__(self, key):
        if database.is_available():
            if redis_cache.get_session(key) is not None:
                return True
            return database.active_session_exists(key)
        return key in self._memory

    def __getitem__(self, key):
        if database.is_available():
            cached = redis_cache.get_session(key)
            if cached is not None:
                return cached
            val = database.get_active_session(key)
            if val is None:
                raise KeyError(key)
            redis_cache.set_session(key, val)  # populate on miss
            return val
        return self._memory[key]

    def __setitem__(self, key, value):
        if database.is_available():
            # Postgres is the source of truth; write it first, then refresh the cache.
            database.save_active_session(key, value)
            redis_cache.set_session(key, value)
        else:
            self._memory[key] = value

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault(self, key, default):
        if key not in self:
            self[key] = default
            return default
        return self[key]

    def keys(self):
        if database.is_available():
            return database.list_active_session_keys()
        return self._memory.keys()

    def values(self):
        if database.is_available():
            return database.list_active_sessions()
        return list(self._memory.values())

    def items(self):
        if database.is_available():
            return [(s["id"], s) for s in database.list_active_sessions()]
        return list(self._memory.items())

    def __delitem__(self, key):
        if database.is_available():
            database.delete_active_session(key)
            redis_cache.delete_session(key)
        else:
            self._memory.pop(key, None)

sessions = DatabaseSessions()

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

# Only load the whole-file snapshot in the no-DB fallback. When the DB is the source of
# truth, returning-candidate history is fetched per-email on demand via get_candidate_previous,
# so loading every candidate into each worker at startup would be wasted RAM and stale.
candidate_history: dict[str, list[dict]] = {} if database.is_available() else _load_history()
if not database.is_available():
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
    ev = session.get("evaluation")
    if ev:
        summary["evaluation"] = {
            "status": ev.get("status"),
            "overall_score": ev.get("overall_score"),
            "recommendation": ev.get("recommendation"),
            "level_fit": ev.get("level_fit"),
        }
    if database.is_available():
        database.save_candidate_history(email, session["id"], summary)
    else:
        candidate_history.setdefault(email, []).append(summary)
        _save_history_to_disk()
    print(f"[History] Saved for {email}: {summary['turns']} turns, session {summary['session_id'][:8]}")


def get_candidate_previous(email: str) -> list[dict]:
    """Get previous sessions for a returning candidate."""
    if database.is_available():
        return database.get_candidate_history(email)
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
    "us.mistral.pixtral-large-2502-v1:0":       (2.00, 6.00),
    "us.amazon.nova-premier-v1:0":              (2.50, 10.00),
}

# STT pricing per minute
_STT_PRICING = {
    "gpt-4o-mini-transcribe": 0.003,
    "whisper-1": 0.006,
    "nova-3": 0.0059,
    "nova-2": 0.0043,
}

# TTS pricing per 1K characters (estimates)
_TTS_PRICING = {
    "deepgram": 0.015,
    "inworld": 0.015,
    "openai": 0.015,
    "kugel": 0.046,  # ~€0.043/min (Turbo) ≈ $0.046/1K chars at ~1000 chars/min English
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


def _get_audio_duration_ms(audio_bytes: bytes, ext: str = "webm") -> int:
    """Get audio duration in ms using ffprobe. Returns 0 if detection fails."""
    import subprocess
    tmp_path = tempfile.mktemp(suffix=f".{ext}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", tmp_path],
            capture_output=True, text=True, timeout=5,
        )
        return int(float(result.stdout.strip()) * 1000) if result.stdout.strip() else 0
    except Exception:
        return 0
    finally:
        try: os.remove(tmp_path)
        except: pass


def _calc_stt_cost(model: str, duration_ms: int) -> float:
    """Calculate STT cost in USD. duration_ms is actual audio length."""
    rate = _STT_PRICING.get(model, 0.006)
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

    # Bedrock streaming (Claude, Llama, Nova, etc.)
    if bedrock_client and (model.startswith("us.") or "anthropic" in model or "amazon" in model or "meta" in model):
        for chunk in _stream_bedrock(messages, model, temperature, max_tokens):
            yield chunk
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


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 24000) -> bytes:
    """Wrap raw PCM s16le bytes in a WAV header. Kugel returns raw PCM, but the
    browser <audio> element and the rest of the pipeline expect a container."""
    import struct
    data_size = len(pcm_bytes)
    byte_rate = sample_rate * 2  # mono, 16-bit
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    header += b"data" + struct.pack("<I", data_size)
    return header + pcm_bytes


def _kugel_tts(text: str, voice: str) -> bytes:
    """Call Kugel TTS and return playable WAV bytes (PCM wrapped). Returns b'' on failure."""
    if not KUGEL_API_KEY or not text.strip():
        return b""
    try:
        voice_id = int(voice) if voice and str(voice).isdigit() else None
    except (TypeError, ValueError):
        voice_id = None
    body = {"text": text[:10000], "model_id": KUGEL_MODEL_ID, "sample_rate": KUGEL_SAMPLE_RATE}
    if voice_id is not None:
        body["voice_id"] = voice_id
    try:
        r = http_requests.post(
            "https://api.kugelaudio.com/v1/tts/generate",
            headers={"Authorization": f"Bearer {KUGEL_API_KEY}",
                     "Content-Type": "application/json; charset=utf-8"},
            json=body, timeout=15,
        )
        r.raise_for_status()
        sr = int(r.headers.get("X-Sample-Rate", KUGEL_SAMPLE_RATE))
        return _pcm_to_wav(r.content, sample_rate=sr)
    except Exception as e:
        print(f"[TTS] Kugel error: {e}")
        return b""


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

    if provider == "kugel" and KUGEL_API_KEY:
        wav = _kugel_tts(text[:2000], voice)
        if wav:
            return wav

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


def _build_bedrock_body(messages, model_id, temperature, max_tokens):
    """Build the request body for a Bedrock model. Returns (body_dict, model_type)
    where model_type is 'claude', 'llama', 'nova', or 'other'."""
    is_claude = "anthropic" in model_id.lower()
    system_text, user_text = "", ""
    for msg in messages:
        if msg["role"] == "system": system_text += msg["content"] + "\n"
        elif msg["role"] == "user": user_text += msg["content"] + "\n"
        elif msg["role"] == "assistant": pass

    if is_claude:
        top_system = ""
        filtered = []
        for msg in messages:
            if msg["role"] == "system":
                if not top_system and not filtered:
                    top_system += msg["content"] + "\n"
                else:
                    filtered.append({"role": "user", "content": msg["content"]})
            else:
                filtered.append(msg)
        if not filtered:
            filtered = [{"role": "user", "content": top_system.strip()}]
            top_system = ""
        merged = []
        for m in filtered:
            if merged and merged[-1]["role"] == m["role"]:
                prev_content = merged[-1]["content"] if isinstance(merged[-1]["content"], str) else merged[-1]["content"]
                new_content = m["content"] if isinstance(m["content"], str) else m["content"]
                merged[-1] = {"role": m["role"], "content": (prev_content if isinstance(prev_content, str) else prev_content) + "\n" + (new_content if isinstance(new_content, str) else new_content)}
            else:
                merged.append(m)
        for i, m in enumerate(merged):
            if isinstance(m.get("content"), str):
                merged[i] = {"role": m["role"], "content": [{"type": "text", "text": m["content"]}]}
        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens, "temperature": temperature, "messages": merged}
        if top_system.strip():
            body["system"] = [{"type": "text", "text": top_system.strip(), "cache_control": {"type": "ephemeral"}}]
        return body, "claude"

    is_llama = "meta" in model_id.lower() or "llama" in model_id.lower()
    is_nova = "amazon" in model_id.lower() or "nova" in model_id.lower()
    if is_llama:
        prompt = ""
        if system_text: prompt += f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n{system_text.strip()}<|eot_id|>"
        prompt += f"<|start_header_id|>user<|end_header_id|>\n\n{user_text.strip()}<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
        return {"prompt": prompt, "max_gen_len": max_tokens, "temperature": temperature}, "llama"
    elif is_nova:
        body = {"inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature}, "messages": [{"role": "user", "content": [{"text": user_text.strip()}]}]}
        if system_text.strip(): body["system"] = [{"text": system_text.strip()}]
        return body, "nova"
    else:
        return {"max_tokens": max_tokens, "temperature": temperature, "messages": messages}, "other"


def _parse_bedrock_response(result_body, model_type):
    """Extract text from a non-streaming Bedrock response."""
    if model_type == "claude": return result_body["content"][0]["text"].strip()
    elif model_type == "llama": return result_body.get("generation", "").strip()
    elif model_type == "nova": return result_body.get("output", {}).get("message", {}).get("content", [{}])[0].get("text", "").strip()
    elif "content" in result_body: return result_body["content"][0]["text"].strip()
    elif "choices" in result_body: return result_body["choices"][0].get("message", {}).get("content", result_body["choices"][0].get("text", "")).strip()
    return json.dumps(result_body)


def _call_bedrock(messages, model_id, temperature, max_tokens):
    """Call AWS Bedrock models (non-streaming)."""
    body, model_type = _build_bedrock_body(messages, model_id, temperature, max_tokens)
    resp = bedrock_client.invoke_model(modelId=model_id, contentType="application/json", accept="application/json", body=json.dumps(body))
    result_body = json.loads(resp["body"].read())
    return _parse_bedrock_response(result_body, model_type)


def _stream_bedrock(messages, model_id, temperature, max_tokens):
    """Stream tokens from AWS Bedrock. Yields text chunks.
    Supports Claude (content_block_delta), Nova (contentBlockDelta),
    and Llama (generation token). Falls back to non-streaming for unknown models."""
    body, model_type = _build_bedrock_body(messages, model_id, temperature, max_tokens)

    try:
        resp = bedrock_client.invoke_model_with_response_stream(
            modelId=model_id, contentType="application/json", accept="application/json",
            body=json.dumps(body))
        stream = resp.get("body")
        if not stream:
            # No stream body — fall back to non-streaming
            full = _call_bedrock(messages, model_id, temperature, max_tokens)
            yield full
            return

        for event in stream:
            chunk = event.get("chunk")
            if not chunk:
                continue
            payload = json.loads(chunk["bytes"])

            if model_type == "claude":
                # Claude streams: contentBlockDelta with delta.text
                if payload.get("type") == "content_block_delta":
                    text = payload.get("delta", {}).get("text", "")
                    if text:
                        yield text
            elif model_type == "nova":
                # Nova streams: contentBlockDelta with delta.text
                delta = payload.get("contentBlockDelta", {}).get("delta", {})
                text = delta.get("text", "")
                if text:
                    yield text
            elif model_type == "llama":
                # Llama streams: generation token
                text = payload.get("generation", "")
                if text:
                    yield text
            else:
                # Unknown model — try common patterns
                text = (payload.get("delta", {}).get("text", "") or
                        payload.get("generation", "") or
                        payload.get("outputText", ""))
                if text:
                    yield text

    except Exception as e:
        print(f"[Bedrock Stream] Streaming failed ({e}), falling back to non-streaming")
        full = _call_bedrock(messages, model_id, temperature, max_tokens)
        yield full


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


def _is_pause_prompt(text: str) -> bool:
    """True if the LLM output is a 'Take your time' style pause prompt.
    These don't count as new questions — the candidate's next answer must attach
    to the ORIGINAL question, not to this prompt."""
    if not text:
        return False
    t = text.strip().lower().strip("\"'").rstrip(".!?,").strip()
    if not t:
        return False
    if t in {"take your time", "please take your time", "ok take your time",
             "alright take your time", "sure take your time", "no rush",
             "take a moment", "please take a moment"}:
        return True
    # Short utterance that's effectively just a pause cue
    return len(t) <= 50 and "take your time" in t


# ── Resume Parsing ───────────────────────────────────────────────────────

def parse_resume(resume_text: str) -> dict:
    if not resume_text or len(resume_text.strip()) < 20:
        return {}
    today_str = datetime.now().strftime("%B %Y")
    prompt = f"""Extract from this resume. Return ONLY valid JSON:
{{"candidate_name":"","email":"","phone":"","level":"fresh_graduate|trained_fresher|experienced_junior|experienced_senior",
"years_experience":0,"skills":[],"tools":[],"key_projects":[],"domain":"","education":""}}

Today's date: {today_str}

Rules:
- email: extract email address if present, empty string if not found
- phone: extract phone number if present, empty string if not found

- years_experience: TOTAL professional/industrial work experience in DECIMAL YEARS.
  Look ONLY inside sections titled (case-insensitive):
    "Experience", "Work Experience", "Professional Experience",
    "Industrial Experience", "Employment", "Career", "Internship", "Internships"
  IGNORE these sections entirely (do NOT count toward experience):
    "Education", "Academic", "Projects", "Personal Projects", "Certifications",
    "Training", "Courses", "Achievements", "Publications"

  Two extraction modes — use BOTH and pick the LARGER value:

  MODE A — DIRECT MENTION:
    If the resume says it explicitly, use that number.
    Examples:
      "5 years of experience"            → 5.0
      "2.5 years in VLSI"                → 2.5
      "6 months internship"              → 0.5
      "1 year 8 months as DV engineer"   → 1.67

  MODE B — COMPUTE FROM DATE RANGES:
    Find every date range under an experience section and SUM the durations.
    A date range looks like:  "<start month/year> – <end month/year>" (any dash: -, –, —, "to")
    Recognize these formats (case-insensitive):
      "June 2025 - Dec 2025"             → 6 months = 0.5
      "Jun 2024 to Present"              → from Jun 2024 to {today_str} (compute)
      "06/2024 – 09/2024"                → 3 months = 0.25
      "2022 - 2024"                      → 24 months = 2.0   (year-only: assume Jan)
      "Aug 2023 - current"               → from Aug 2023 to today
    "Present", "Current", "Now", "Till date", "Ongoing" all mean {today_str}.
    Compute: months = (end_year - start_year) * 12 + (end_month - start_month)
    Sum across ALL listed positions, then divide by 12 for years (round to 2 decimals).
    DO NOT count overlapping ranges twice — if two roles overlap, only count the union.
    DO NOT count internships that are listed under "Projects" or "Training".

  Final value = max(MODE A result, MODE B result).
  If candidate is clearly a fresher / student with NO work or internship dates → 0.
  NEVER confuse months with years (6 months is 0.5, NOT 6).
  Cap the value at 50.

- level: classify based on years_experience and resume content
    fresh_graduate     = 0 years, no internship
    trained_fresher    = 0–1 year, has internship/training only
    experienced_junior = 1–5 years professional
    experienced_senior = 5+ years professional

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

# Deepgram keywords format: "word:boost" (boost 1-10). Multi-word entries
# must be URL-encoded — unencoded spaces would make the query string invalid → 400.
_DG_KEYWORDS = "&".join(f"keywords={quote(k)}:5" for k in VLSI_KEYWORDS[:50])

# Generic STT prompt — fallback when domain is unknown or its file is missing.
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

# Per-domain STT prompt cache (loaded lazily from stt_prompts/<domain>.txt).
# Whisper / gpt-4o-mini-transcribe prompt is capped at 224 tokens, so each
# domain file is sized to fit within that budget with VLSI vocabulary only
# relevant to that domain — giving better biasing than a generic prompt.
_STT_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "stt_prompts")
_STT_PROMPT_CACHE: dict[str, str] = {}

def _get_stt_prompt(domain: str) -> str:
    """Return the STT prompt for the given domain (cached). Falls back to the generic prompt."""
    if not domain:
        return _OPENAI_STT_PROMPT
    if domain in _STT_PROMPT_CACHE:
        return _STT_PROMPT_CACHE[domain]
    path = os.path.join(_STT_PROMPT_DIR, f"{domain}.txt")
    try:
        with open(path, "r") as f:
            text = f.read().strip()
        if text:
            _STT_PROMPT_CACHE[domain] = text
            print(f"[STT] Loaded domain prompt: {domain} ({len(text)} chars)")
            return text
    except FileNotFoundError:
        print(f"[STT] No prompt file for domain '{domain}' — using generic")
    except Exception as e:
        print(f"[STT] Failed to load prompt for '{domain}': {e}")
    _STT_PROMPT_CACHE[domain] = _OPENAI_STT_PROMPT
    return _OPENAI_STT_PROMPT


def transcribe_audio(audio_bytes: bytes, ext: str = "webm", domain: str = "") -> tuple[str, int]:
    """Returns (transcript, latency_ms)."""
    provider = RUNTIME_CONFIG.get("stt_provider", "openai")
    model = RUNTIME_CONFIG.get("stt_model", "gpt-4o-mini-transcribe")
    tmp_path = None
    t0 = time.time()

    # Deepgram STT with VLSI keyword boosting
    if provider == "deepgram" and DEEPGRAM_API_KEY:
        try:
            url = f"https://api.deepgram.com/v1/listen?model={model}&language=en&smart_format=true&{_DG_KEYWORDS}"
            r = http_requests.post(url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": f"audio/{ext}"},
                data=audio_bytes, timeout=15)
            r.raise_for_status()
            data = r.json()
            alt = data.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0]
            text = alt.get("transcript", "").strip()
            latency = round((time.time() - t0) * 1000)
            print(f"[STT] Deepgram/{model} {latency}ms — {len(text)} chars")
            return text, latency
        except Exception as e:
            print(f"[STT] Deepgram error: {e}, falling back to OpenAI")

    # OpenAI STT with VLSI domain prompt — domain-specific if provided, generic otherwise.
    stt_prompt = _get_stt_prompt(domain)
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
            f.write(audio_bytes); tmp_path = f.name
        with open(tmp_path, "rb") as audio_file:
            response = openai_client.audio.transcriptions.create(
                model=model if provider == "openai" else "gpt-4o-mini-transcribe",
                file=audio_file, language="en",
                prompt=stt_prompt,
            )
        latency = round((time.time() - t0) * 1000)
        text = response.text.strip() if hasattr(response, "text") else str(response).strip()
        # Filter STT hallucinations — model echoes prompt hint when audio is silent.
        # Check the actual prompt used (every domain prompt starts with "This is a VLSI"
        # and contains "Common terms:" so those substrings cover all variants).
        if text and ("This is a VLSI" in text or "VLSI semiconductor" in text or "Common terms:" in text):
            print(f"[STT] OpenAI/{model} {latency}ms — HALLUCINATION filtered (prompt echo)")
            return "", latency
        print(f"[STT] OpenAI/{model} {latency}ms — {len(text)} chars (domain={domain or 'generic'})")
        return text, latency
    except Exception as e:
        print(f"[STT] Error: {e}")
        return "", round((time.time() - t0) * 1000)
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except: pass


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket):
    """WebSocket for audio streaming — browser handles VAD."""
    await ws.accept()
    sid = ws.query_params.get("session_id", "")
    await ws.send_json({"event": "connected", "vad": "browser"})
    print(f"[WS] Audio stream connected (session={sid}, vad=browser)")

    try:
        while True:
            await ws.receive_bytes()
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

    # Kugel
    if provider == "kugel" and KUGEL_API_KEY:
        wav = _kugel_tts(text[:2000], voice)
        latency = round((time.time() - t0) * 1000)
        if wav:
            print(f"[TTS] Kugel {latency}ms — {len(text)} chars (voice={voice})")
            return base64.b64encode(wav).decode(), latency

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
    """Load prompt from file. fresh_graduate and trained_fresher both use the
    experienced_junior prompts — the easier 'what is X' prompts were dropped
    because trained-fresher questions were testing too low. Falls back to
    experienced_junior_physical_design if the requested file is missing."""
    if level in ("fresh_graduate", "trained_fresher"):
        level = "experienced_junior"
    filename = f"{level}_{domain}.md"
    filepath = os.path.join(_PROMPTS_DIR, filename)
    try:
        with open(filepath, "r") as f:
            prompt = f.read()
        print(f"[Prompt] Loaded {filename}")
        return prompt
    except FileNotFoundError:
        print(f"[Prompt] {filename} not found, falling back to experienced_junior_physical_design.md")
        fallback = os.path.join(_PROMPTS_DIR, "experienced_junior_physical_design.md")
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
_BASE = _load_prompt("experienced_junior", "physical_design")


def generate_expected_points(question: str, domain: str, level: str, session: dict):
    """Background LLM call to generate expected key points for a question.
    Stores result in the conversation entry so the next turn's prompt can
    inject them, giving the interviewer concrete points to probe."""
    if not question or _is_pause_prompt(question):
        return
    # Skip greetings / non-technical questions
    q_lower = question.lower()
    if any(phrase in q_lower for phrase in ["tell me about yourself", "introduce yourself",
                                            "thank you", "welcome", "good morning",
                                            "good afternoon", "good evening"]):
        return

    # System message is static per domain+level — gets cached by Bedrock/Claude
    system_msg = (f"You are a VLSI {domain.replace('_', ' ')} expert evaluator. "
                  f"For each interview question, list 3-5 KEY POINTS expected in a good answer "
                  f"from a {level.replace('_', ' ')} candidate. "
                  f"Each point must be 1 short sentence (under 15 words). "
                  f"Return ONLY a valid JSON array, no markdown. Example: [\"point 1\", \"point 2\"]")
    user_msg = f'Question: "{question}"'

    try:
        t0 = time.time()
        raw, usage = call_llm([{"role": "system", "content": system_msg},
                               {"role": "user", "content": user_msg}],
                              temperature=0.0, max_tokens=300)
        ms = round((time.time() - t0) * 1000)
        points = safe_json(raw)
        # Fallback: if truncated JSON array, try to salvage complete entries
        if points is None and "[" in raw:
            truncated = re.sub(r'```json|```', '', raw).strip()
            # Find last complete string entry before truncation
            last_quote = truncated.rfind('"')
            if last_quote > 0:
                candidate = truncated[:last_quote + 1].rstrip().rstrip(',') + "]"
                if not candidate.startswith("["):
                    candidate = "[" + candidate.split("[", 1)[-1]
                points = safe_json(candidate)
        if isinstance(points, list) and points:
            # Store in the matching conversation entry
            for entry in reversed(session.get("conversation", [])):
                if entry.get("question") == question:
                    entry["expected_points"] = points
                    break
            # Persist to DB so the points survive between requests
            if database.is_available():
                database.save_active_session(session["id"], session)
            print(f"[ExpectedPts] {ms}ms | {len(points)} points | ${usage['cost_usd']:.4f}")
        session.setdefault("obs_log", []).append(
            _obs_entry("LLM_expected_points", RUNTIME_CONFIG["qgen_model"], ms, "success",
                       input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                       cost_usd=usage["cost_usd"]))
    except Exception as e:
        print(f"[ExpectedPts] Failed: {e}")



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
            # Filter: only keep actual technical questions, skip greetings/corrections/closings
            _skip_phrases = {"good morning", "good afternoon", "good evening", "tell me about yourself",
                             "welcome back", "thanks for coming", "don't go personal", "let's focus",
                             "please answer in english", "take your time", "that covers what i needed",
                             "thank you for your time", "i'll decide what to ask", "let's continue",
                             "let's move on"}
            for ps in recent:
                for q in ps.get("questions_asked", []):
                    q_lower = q.strip().lower()
                    # Skip if it starts with or is dominated by a non-question phrase
                    if any(q_lower.startswith(p) for p in _skip_phrases):
                        continue
                    if len(q_lower) < 15:  # too short to be a real question
                        continue
                    prev_questions.append(q)
                for p in ps.get("projects", []):
                    prev_projects.add(str(p))

            projects_note = ""
            if prev_projects:
                projects_note = f"\nProjects discussed before: {', '.join(prev_projects)}\nAsk about DIFFERENT aspects of these projects, or explore projects not yet discussed."

            returning_block = f"""
RETURNING CANDIDATE: This candidate has interviewed {len(prev_sessions)} time(s) before.
These questions were already asked in previous sessions:
{chr(10).join(f'- {q}' for q in prev_questions)}{projects_note}
This is a completely NEW interview. Ask fresh questions from different angles on the same topics.
Test whether the candidate has genuinely improved or just memorized answers from before."""

    system = base_prompt + candidate_info + returning_block

    messages = [{"role": "system", "content": system}]
    # Add conversation history — inject expected points when available
    for entry in history[-10:]:
        if entry.get("question"):
            messages.append({"role": "assistant", "content": entry["question"]})
        # Inject expected points BEFORE the candidate's answer so the interviewer
        # knows what to look for when reading the answer
        if entry.get("expected_points") and entry.get("answer"):
            pts = ", ".join(entry["expected_points"])
            messages.append({"role": "system", "content":
                f"EXPECTED POINTS for your last question: {pts}\n"
                "Check which points the candidate covers below. Probe MISSING points before moving on."})
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


SESSION_MAX_DURATION_SEC = int(os.getenv("SESSION_MAX_DURATION_SEC", "3600"))  # 1 hour

def _should_end_interview(session) -> tuple[bool, str]:
    """Hard limit: turn count and session duration. Early end decided by LLM via system prompt."""
    if session.get("turn", 0) >= 25:
        return True, "That's all from my side. Thank you for your time."
    # Auto-end after 1 hour
    started = session.get("started_at", 0)
    if started and (time.time() - started) > SESSION_MAX_DURATION_SEC:
        return True, "We've run out of time. Thank you for your time."
    return False, ""


# ── Candidate Behavior Guard ───────────────────────────────────────────

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
    if not answer or len(answer.split()) < 20:
        return  # Too short for reliable detection

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

    # Store result in conversation entry. This runs in a background thread, so persist
    # via a targeted jsonb_set (not a full-session writeback) — a full overwrite here
    # would race the foreground turn handler and could clobber it or tear json.dumps.
    if turn_index < len(session.get("conversation", [])):
        session["conversation"][turn_index]["ai_detection"] = result
        if database.is_available():
            database.update_session_ai_detection(session["id"], turn_index, result)
            # This jsonb_set bypassed the cache; drop the stale blob so the next
            # read repopulates from Postgres (which now has this ai_detection).
            redis_cache.delete_session(session["id"])

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
    question, usage = call_llm(messages, temperature=0.7, max_tokens=200)
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

    # Pause prompt ("Take your time") — don't count as a new question.
    is_pause_prompt = _is_pause_prompt(question)
    if is_pause_prompt:
        print(f"[Submit] Pause prompt detected — not counting as a turn: \"{question}\"")
    else:
        session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
        session["turn"] += 1

    # Store LLM timing + cost
    session.setdefault("obs_log", []).append(obs)

    # Fire background expected-points generation for the new question
    if not llm_end and not is_pause_prompt:
        resume = session.get("resume", {})
        domain = resume.get("domain", "physical_design")
        level = resume.get("level", "trained_fresher")
        threading.Thread(target=generate_expected_points,
                         args=(question, domain, level, session), daemon=True).start()

    return {"question": question, "should_end": llm_end, "pause_prompt": is_pause_prompt, "llm_ms": llm_ms}


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

    context = f"""Generate a short opening greeting for a technical interview.

Your name: Ranjitha
Time: {time_of_day}
Candidate name: {call_name}
Returning: {'yes, interviewed ' + str(len(prev_sessions)) + ' time(s) before' if prev_sessions else 'no, first time'}

Rules:
- Maximum 1 sentence, 8-20 words. Never more than 20 words.
- Introduce yourself by name, greet them, ask them to introduce themselves
- First time: "Good evening Veera, I'm Ranjitha. Tell me about yourself."
- Returning: "Good evening Veera, I'm Ranjitha. Thank you for joining again, tell me about yourself."
- Plain spoken. No "thanks so much", "before we dive in", "why don't you" or scripted phrases.
- Do NOT ask technical questions yet
- Do NOT mention domain, scoring, or evaluation
- If returning: acknowledge they're back, don't reveal previous scores
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
        greeting = f"Good {time_of_day}{' ' + name if name else ''}, I'm Ranjitha. Tell me about yourself."

    if prev_sessions:
        session["is_returning"] = True
        session["previous_sessions"] = len(prev_sessions)

    session["conversation"].append({"question": greeting, "answer": None, "turn": 0})
    return greeting


# ── End-of-Interview Evaluation ─────────────────────────────────────────
# Runs ONCE when the interview ends, and ONLY if the candidate actually
# answered enough questions to be judged fairly. Scores the whole transcript
# with a rubric tuned to the candidate's level.

MIN_ANSWERS_FOR_EVAL = int(os.getenv("MIN_ANSWERS_FOR_EVAL", "8"))
EVAL_SWEEP_INTERVAL_SEC = int(os.getenv("EVAL_SWEEP_INTERVAL_SEC", "300"))  # how often the sweeper wakes
EVAL_SWEEP_GRACE_SEC = int(os.getenv("EVAL_SWEEP_GRACE_SEC", "120"))        # don't touch a session updated more recently than this

# Shared output contract. Kept separate from the rubric text so the {...} braces
# here never collide with prompt placeholders (we fill with str.replace, not
# str.format, precisely so these JSON braces survive untouched).
_EVAL_JSON_SCHEMA = """{
  "overall_score": <integer 0-10>,
  "recommendation": "strong_yes|yes|maybe|no|strong_no",
  "level_fit": "below_level|at_level|above_level",
  "verdict": "one-line hire verdict",
  "per_question": [{"q": <question number>, "question": "<first ~10 words>", "score": <0-10>, "comment": "one short clause", "expected_points": ["point 1", "point 2"], "missing_points": ["point 3"]}],
  "communication_score": <integer 0-10>,
  "communication": "1-2 sentences on clarity, structure, and how they explain reasoning",
  "strengths": ["short bullet", "..."],
  "weaknesses": ["short bullet", "..."],
  "topic_breakdown": [{"topic": "", "score": <0-10>, "comment": ""}],
  "red_flags": ["only genuine concerns; [] if none"],
  "summary": "3-4 sentence overall assessment"
}"""

# Appended to every level rubric: the numbered transcript, the per-question and
# communication scoring instructions, and the JSON contract.
_EVAL_TASK = """FULL TRANSCRIPT (each question is numbered [Q1], [Q2], ...; [A1] is the answer to [Q1]):
{transcript}

In addition to the overall assessment, do BOTH of these:
- Score EVERY numbered question individually in "per_question", referencing its number. Judge each answer's technical merit at THIS candidate's level.
- For each question in "per_question", populate:
  - "expected_points": A list of key technical concepts/keywords expected in a correct answer.
  - "missing_points": ONLY the concepts the candidate did NOT cover or got wrong. If the candidate mentioned a concept correctly (even partially), do NOT include it in missing_points. Compare the answer carefully against each expected point — give credit for correct mentions. Use an empty list [] if they covered all expected points.
CRITICAL: "missing_points" must be a STRICT SUBSET of "expected_points". Never copy expected_points into missing_points blindly. Read the candidate's answer word by word — if they mentioned a concept, remove it from missing.
- Score the candidate's COMMUNICATION skills 0-10 in "communication_score": clarity, structure, conciseness, and how well they explain their reasoning. Judge HOW they communicate, independent of technical correctness.

Return ONLY valid JSON, no prose, no markdown fences:
""" + _EVAL_JSON_SCHEMA

_EVAL_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_prompts")

def _load_eval_prompt(level: str) -> str:
    """Load level-specific rubric from eval_prompts/{level}.md and append the
    shared task + JSON schema. Keeping _EVAL_TASK inline (not in the file)
    means the JSON contract has a single source of truth."""
    path = os.path.join(_EVAL_PROMPTS_DIR, f"{level}.md")
    with open(path, "r", encoding="utf-8") as f:
        rubric = f.read().rstrip() + "\n\n"
    return rubric + _EVAL_TASK

EVAL_PROMPTS = {
    "experienced_junior": _load_eval_prompt("experienced_junior"),
    "experienced_senior": _load_eval_prompt("experienced_senior"),
}
# Pristine copies for the admin "Reset to Default" action.
_DEFAULT_EVAL_PROMPTS = dict(EVAL_PROMPTS)


def get_eval_prompt(level: str) -> str:
    """Pick the eval prompt for a level. fresh_graduate and trained_fresher both
    use the experienced_junior rubric, matching how _load_prompt maps them."""
    if level in ("fresh_graduate", "trained_fresher"):
        level = "experienced_junior"
    return EVAL_PROMPTS.get(level, EVAL_PROMPTS["experienced_junior"])


def _fill_eval_prompt(template: str, **kw) -> str:
    """Substitute {key} placeholders without str.format — the JSON schema in the
    template contains literal braces that str.format would choke on."""
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _answered_count(session) -> int:
    """How many questions the candidate actually answered (non-empty answer)."""
    return sum(1 for e in session.get("conversation", [])
               if (e.get("answer") or "").strip())


def _build_eval_transcript(session, max_chars: int = 25000) -> str:
    """Render the conversation as numbered Q/A pairs so the evaluator can map a
    per-question score back to each question by its number."""
    lines = []
    n = 0
    for e in session.get("conversation", []):
        q = (e.get("question") or "").strip()
        if not q:
            continue
        n += 1
        a = (e.get("answer") or "").strip()
        lines.append(f"[Q{n}] {q}")
        lines.append(f"[A{n}] {a if a else '(no answer)'}")
    return "\n".join(lines)[:max_chars]


_eval_locks = {}  # per-session locks to prevent duplicate evaluation

def evaluate_interview(session) -> dict:
    """Score the full interview once it ends. Gated on MIN_ANSWERS_FOR_EVAL.
    Idempotent (skips if already evaluated) and safe to run in a background
    thread — it persists via targeted jsonb_set, never a full session writeback."""
    sid = session.get("id", "")

    # Per-session lock to prevent race between async eval and sweeper
    if sid not in _eval_locks:
        _eval_locks[sid] = threading.Lock()
    if not _eval_locks[sid].acquire(blocking=False):
        existing = session.get("evaluation")
        return existing or {"status": "skipped", "reason": "evaluation already in progress"}

    try:
        # Don't evaluate twice (e.g. should_end fires, then the client calls /end-session).
        existing = session.get("evaluation")
        if existing and existing.get("status") in ("done", "skipped"):
            return existing

        answered = _answered_count(session)
        if answered < MIN_ANSWERS_FOR_EVAL:
            result = {"status": "skipped", "answered": answered,
                      "reason": f"only {answered} answered (need {MIN_ANSWERS_FOR_EVAL})"}
            session["evaluation"] = result
            if database.is_available():
                database.save_session_evaluation(sid, result)
                redis_cache.delete_session(sid)
            print(f"[Eval] Skipped {sid[:8]} — {answered}/{MIN_ANSWERS_FOR_EVAL} answered")
            return result

        resume = session.get("resume", {})
        level = resume.get("level", "trained_fresher")
        prompt = _fill_eval_prompt(
            get_eval_prompt(level),
            name=resume.get("candidate_name", "Candidate"),
            domain=str(resume.get("domain", "VLSI")).replace("_", " "),
            level=level.replace("_", " "),
            years=resume.get("years_experience", 0),
            num_answers=answered,
            transcript=_build_eval_transcript(session),
        )

        model = RUNTIME_CONFIG.get("eval_model", "gpt-4o-mini")
        transcript = _build_eval_transcript(session)
        print(f"[Eval] {sid[:8]} — transcript length: {len(transcript)} chars, answered: {answered}, level: {level}, model: {model}")
        t0 = time.time()
        try:
            raw, usage = call_llm([{"role": "user", "content": prompt}],
                                  model_id=model, temperature=0.2, max_tokens=4000)
        except Exception as e:
            print(f"[Eval] LLM call failed for {sid[:8]}: {e}")
            result = {"status": "error", "answered": answered, "error": str(e)}
            session["evaluation"] = result
            if database.is_available():
                database.save_session_evaluation(sid, result)
                redis_cache.delete_session(sid)
            return result
        eval_ms = round((time.time() - t0) * 1000)

        print(f"[Eval] {sid[:8]} — raw response length: {len(raw)} chars, first 300: {raw[:300]}")
        parsed = safe_json(raw) or {}
        result = {
            "status": "done",
            "answered": answered,
            "level": level,
            "model": model,
            "latency_ms": eval_ms,
            "cost_usd": round(usage["cost_usd"], 6),
            "ts": time.time(),
            **parsed,
        }
        if not parsed:
            result["parse_error"] = True
            result["raw_response"] = raw[:2000]
            print(f"[Eval] {sid[:8]} — PARSE FAILED! Raw response:\n{raw[:1000]}")

        session["evaluation"] = result

        obs = _obs_entry("LLM_evaluation", model, eval_ms, "success" if parsed else "failure",
                         input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                         cost_usd=usage["cost_usd"])
        session.setdefault("obs_log", []).append(obs)

        if database.is_available():
            database.save_session_evaluation(sid, result)
            database.append_session_obs(sid, obs)
            redis_cache.delete_session(sid)  # these jsonb_set writes bypassed the cache

        print(f"[Eval] {sid[:8]} ({level}): score={result.get('overall_score', '?')} "
              f"rec={result.get('recommendation', '?')} {eval_ms}ms ${usage['cost_usd']:.4f}")

        # Send results to LMS if this was an LMS-launched session
        if session.get("lms_callback_url"):
            threading.Thread(target=_lms_callback, args=(session,), daemon=True).start()

        return result
    finally:
        _eval_locks.pop(sid, None)


def _evaluate_async(session):
    """Run evaluation off the request path so the candidate's closing message
    isn't delayed. Call only AFTER the final full-session writeback, so the
    thread's targeted jsonb_set lands on top of it rather than being overwritten."""
    threading.Thread(target=evaluate_interview, args=(session,), daemon=True).start()


def _eval_sweeper_loop():
    """Catch sessions whose foreground eval never ran (browser closed, worker
    restart, missing [END_INTERVIEW]). Picks up any ended session without an
    evaluation that's been idle past the grace window."""
    while True:
        try:
            time.sleep(EVAL_SWEEP_INTERVAL_SEC)
            if not database.is_available():
                continue
            pending = database.list_ended_sessions_needing_eval(EVAL_SWEEP_GRACE_SEC)
            if pending:
                print(f"[EvalSweep] {len(pending)} ended session(s) need evaluation")
            for sess in pending:
                try:
                    evaluate_interview(sess)
                except Exception as e:
                    print(f"[EvalSweep] {sess.get('id', '?')[:8]} failed: {e}")
        except Exception as e:
            print(f"[EvalSweep] loop error: {e}")


threading.Thread(target=_eval_sweeper_loop, daemon=True, name="eval-sweeper").start()


STALE_SESSION_SEC = int(os.getenv("STALE_SESSION_SEC", "3600"))  # 1 hour

def _stale_session_sweeper():
    """End sessions that have been inactive for over 1 hour.
    Catches candidates who disconnected mid-interview, browser closed, etc.
    Runs every 5 minutes alongside the eval sweeper."""
    while True:
        try:
            time.sleep(300)  # check every 5 minutes
            if not database.is_available():
                continue
            import json as _json
            with database.get_conn() as conn:
                with conn.cursor() as cur:
                    # Find active/interview/greeting sessions older than STALE_SESSION_SEC
                    cur.execute("""
                        SELECT session_id, session_data FROM active_sessions
                        WHERE session_data->>'phase' NOT IN ('ended')
                          AND updated_at < NOW() - (%s || ' seconds')::interval
                        LIMIT 20
                    """, (str(STALE_SESSION_SEC),))
                    stale = cur.fetchall()
            if not stale:
                continue
            print(f"[StaleSweep] Found {len(stale)} stale session(s) to end")
            for sid, data in stale:
                try:
                    if isinstance(data, str):
                        data = _json.loads(data)
                    turns = data.get("turn", 0)
                    name = data.get("resume", {}).get("candidate_name", "?")
                    data["phase"] = "ended"
                    data["end_reason"] = "stale_timeout"
                    database.save_active_session(sid, data)
                    print(f"[StaleSweep] Ended {sid[:8]} ({name}, turn {turns}) — inactive > {STALE_SESSION_SEC}s")
                    # Trigger evaluation if enough turns
                    if turns >= MIN_ANSWERS_FOR_EVAL:
                        threading.Thread(target=evaluate_interview, args=(data,), daemon=True).start()
                except Exception as e:
                    print(f"[StaleSweep] Failed to end {sid[:8]}: {e}")
        except Exception as e:
            print(f"[StaleSweep] loop error: {e}")


threading.Thread(target=_stale_session_sweeper, daemon=True, name="stale-sweeper").start()


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


# ── Speaker Verification (Resemblyzer only) ─────────────────────────────
_resemblyzer_encoder = None

def _get_resemblyzer_encoder():
    global _resemblyzer_encoder
    if _resemblyzer_encoder is None:
        from resemblyzer import VoiceEncoder
        _resemblyzer_encoder = VoiceEncoder()
        print("[Speaker] Resemblyzer encoder loaded")
    return _resemblyzer_encoder

def _to_wav16k(audio_bytes):
    """Convert webm/any audio → 16kHz mono via ffmpeg. Returns (numpy_array, torch_tensor)."""
    import torch, soundfile as sf, subprocess
    in_path = tempfile.mktemp(suffix=".webm")
    out_path = tempfile.mktemp(suffix=".wav")
    try:
        with open(in_path, "wb") as f:
            f.write(audio_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", "-f", "wav", out_path],
            capture_output=True, timeout=10,
        )
        data, sr = sf.read(out_path, dtype="float32")
        return data, torch.from_numpy(data).unsqueeze(0)
    finally:
        for p in (in_path, out_path):
            try: os.remove(p)
            except: pass

# ── Background Speaker Verification (anti-cheat) ─────────────────────────

import random as _random

SPEAKER_VERIFY_THRESHOLD = 0.80  # Resemblyzer cosine similarity threshold

def _compute_speaker_embedding(audio_bytes):
    """Compute Resemblyzer 256-dim embedding from audio bytes. Returns numpy array or None."""
    try:
        from resemblyzer import preprocess_wav
        np_audio, _ = _to_wav16k(audio_bytes)
        encoder = _get_resemblyzer_encoder()
        processed = preprocess_wav(np_audio)
        embedding = encoder.embed_utterance(processed)
        return embedding
    except Exception as e:
        print(f"[SpeakerVerify] Embedding error: {e}")
        return None


def _verify_speaker_background(audio_bytes, session, turn):
    """Background speaker verification using Resemblyzer (256-dim).
    - Turn 1: Store reference embedding (if not provided by LMS)
    - Random turns: Compare and flag mismatch
    """
    import numpy as np
    sid = session.get("id", "?")[:8]

    try:
        # If session already ended or flagged, skip
        if session.get("phase") == "ended" or session.get("speaker_mismatch"):
            return

        # Compute embedding for current audio
        current_emb = _compute_speaker_embedding(audio_bytes)
        if current_emb is None:
            return

        # Turn 1: Store reference embedding
        if "speaker_ref_embedding" not in session:
            # Use LMS-provided voice if available
            if session.get("user_voice_ref"):
                ref_emb = _compute_speaker_embedding(session["user_voice_ref"])
                if ref_emb is not None:
                    session["speaker_ref_embedding"] = ref_emb
                    print(f"[SpeakerVerify] {sid} — Reference from LMS voice (256-dim)")
                    # Also verify first answer against LMS reference
                    score = float(np.dot(ref_emb, current_emb) /
                                  (np.linalg.norm(ref_emb) * np.linalg.norm(current_emb)))
                    print(f"[SpeakerVerify] {sid} — Turn {turn} score: {score:.4f}")
                    if score < SPEAKER_VERIFY_THRESHOLD:
                        _flag_speaker_mismatch(session, turn, score)
                    return
            # No LMS voice — use first answer as reference
            session["speaker_ref_embedding"] = current_emb
            print(f"[SpeakerVerify] {sid} — Reference from turn 1 (256-dim)")
            return

        # Subsequent turns: compare against reference
        ref_emb = session["speaker_ref_embedding"]
        score = float(np.dot(ref_emb, current_emb) /
                      (np.linalg.norm(ref_emb) * np.linalg.norm(current_emb)))
        print(f"[SpeakerVerify] {sid} — Turn {turn} score: {score:.4f}")

        if score < SPEAKER_VERIFY_THRESHOLD:
            _flag_speaker_mismatch(session, turn, score)

    except Exception as e:
        print(f"[SpeakerVerify] {sid} — Error: {e}")


def _flag_speaker_mismatch(session, turn, score):
    """Flag session as speaker mismatch — ends interview."""
    sid = session.get("id", "?")[:8]
    session["speaker_mismatch"] = {
        "detected_at_turn": turn,
        "score": round(score, 4),
        "threshold": SPEAKER_VERIFY_THRESHOLD,
        "timestamp": time.time(),
    }
    session["phase"] = "ended"
    session["end_reason"] = "speaker_mismatch"
    sessions[session["id"]] = session
    print(f"[SpeakerVerify] {sid} — MISMATCH at turn {turn} (score={score:.4f} < {SPEAKER_VERIFY_THRESHOLD}) — INTERVIEW ENDED")


def _should_run_speaker_check(session, turn):
    """Decide whether to run speaker verification on this turn.
    - Always on turn 1 (to store/verify reference)
    - Random ~40% chance on other turns (to avoid overhead every turn)
    """
    if turn <= 1:
        return True
    if session.get("phase") == "ended":
        return False
    return _random.random() < 0.4


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


# ── LMS Integration ──────────────────────────────────────────────────────

def _extract_resume_text(content: bytes, filename: str) -> str:
    """Extract text from resume file (PDF/DOCX/TXT). Reuses parse-resume logic."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
    text = ""

    if ext == "pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(400, "Not a valid PDF.")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content); tmp_path = tmp.name
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    for page in pdf.pages:
                        text += (page.extract_text() or "") + "\n"
            except Exception:
                pass
            if not text.strip():
                try:
                    import fitz
                    textract_client = boto3.client("textract",
                        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
                    doc = fitz.open(tmp_path)
                    for i in range(min(len(doc), 3)):
                        single = fitz.open()
                        single.insert_pdf(doc, from_page=i, to_page=i)
                        resp = textract_client.analyze_document(
                            Document={"Bytes": single.tobytes()}, FeatureTypes=["LAYOUT"])
                        single.close()
                        text += "\n".join(b["Text"] for b in resp.get("Blocks", []) if b["BlockType"] == "LINE") + "\n"
                    doc.close()
                except Exception:
                    pass
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except: pass
    elif ext in ("docx", "doc"):
        import docx2txt
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(content); tmp_path = tmp.name
        text = docx2txt.process(tmp_path)
        os.unlink(tmp_path)
    else:
        text = content.decode("utf-8", errors="ignore")

    return text.strip()


@app.post("/api/lms/launch")
async def lms_launch(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    domain: str = Form("physical_design"),
    resume: UploadFile = File(...),
    callback_url: str = Form(""),
    user_voice: UploadFile = File(None),
):
    """LMS calls this to create a session and get a signed launch URL."""
    api_key = request.headers.get("X-API-Key", "")
    if not LMS_API_KEY or api_key != LMS_API_KEY:
        raise HTTPException(401, "Invalid or missing API key")

    content = await resume.read()
    if len(content) > 5_000_000:
        raise HTTPException(413, "Resume too large. Max 5MB.")
    text = _extract_resume_text(content, resume.filename or "resume.pdf")
    if not text:
        raise HTTPException(400, "Could not extract text from resume.")

    parsed = parse_resume(text)
    parsed["candidate_name"] = name
    parsed["email"] = email
    parsed["domain"] = domain
    parsed["resume_text"] = text[:3000]

    # Process user voice reference (for speaker verification)
    voice_bytes = None
    if user_voice and user_voice.filename:
        voice_bytes = await user_voice.read()
        if len(voice_bytes) > 10_000_000:
            raise HTTPException(413, "Voice file too large. Max 10MB.")

    sid = secrets.token_hex(8)
    session = {
        "id": sid, "mode": "mock", "resume": parsed, "phase": "greeting",
        "turn": 0, "conversation": [], "started_at": time.time(),
        "difficulty_level": 1, "lms_source": True,
    }
    if callback_url:
        session["lms_callback_url"] = callback_url
    if voice_bytes:
        session["user_voice_ref"] = voice_bytes
    sessions[sid] = session

    token = jwt.encode({
        "type": "lms_launch",
        "sid": sid,
        "email": email,
        "exp": datetime.now(tz=timezone.utc) + timedelta(minutes=30),
    }, JWT_SECRET, algorithm="HS256")

    host = request.headers.get("host", request.base_url.hostname)
    scheme = request.headers.get("x-forwarded-proto", "https")
    launch_url = f"{scheme}://{host}/?lms=1&token={token}&session_id={sid}"

    print(f"[LMS] Launch session {sid[:8]} for {name} ({email}), domain={domain}")

    # If request wants JSON (API call), return JSON; otherwise redirect to lobby
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return {"session_id": sid, "launch_url": launch_url, "resume": parsed}

    from starlette.responses import RedirectResponse
    return RedirectResponse(launch_url, status_code=303)


def _lms_callback(session):
    """POST evaluation results back to LMS callback URL (fire-and-forget)."""
    url = session.get("lms_callback_url")
    if not url:
        return
    evaluation = session.get("evaluation", {})
    resume = session.get("resume", {})
    payload = {
        "session_id": session.get("id"),
        "email": resume.get("email", ""),
        "name": resume.get("candidate_name", ""),
        "status": evaluation.get("status", "error"),
        "overall_score": evaluation.get("overall_score"),
        "communication_score": evaluation.get("communication_score"),
        "verdict": evaluation.get("verdict"),
        "level_fit": evaluation.get("level_fit"),
        "summary": evaluation.get("summary", ""),
        "strengths": evaluation.get("strengths", []),
        "weaknesses": evaluation.get("weaknesses", []),
    }
    try:
        resp = http_requests.post(url, json=payload,
                                  headers={"X-API-Key": LMS_API_KEY},
                                  timeout=10)
        print(f"[LMS] Callback to {url} — {resp.status_code}")
    except Exception as e:
        print(f"[LMS] Callback failed: {e}")


# ── Resume Parsing ───────────────────────────────────────────────────────

@app.post("/api/parse-resume")
def parse_resume_endpoint(file: UploadFile = File(...)):
    content = file.file.read()
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
            t0_pdf = time.time()
            print(f"[Resume] PDF upload: {len(content)} bytes, file={file.filename}")
            # Try pdfplumber first (text-based PDFs)
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    for page in pdf.pages:
                        text += (page.extract_text() or "") + "\n"
                if text.strip():
                    print(f"[Resume] pdfplumber extracted {len(text.strip())} chars ({round((time.time()-t0_pdf)*1000)}ms)")
            except Exception as e:
                print(f"[Resume] pdfplumber failed: {e}")
            # Amazon Textract for scanned/image PDFs — send page-by-page as single-page PDFs
            if not text.strip():
                try:
                    import fitz  # PyMuPDF — split pages
                    textract_client = boto3.client("textract",
                        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
                    doc = fitz.open(tmp_path)
                    num_pages = min(len(doc), 3)  # max 3 pages for resume
                    for i in range(num_pages):
                        single = fitz.open()
                        single.insert_pdf(doc, from_page=i, to_page=i)
                        pdf_bytes = single.tobytes()
                        single.close()
                        resp = textract_client.analyze_document(
                            Document={"Bytes": pdf_bytes},
                            FeatureTypes=["LAYOUT"])
                        lines = [b["Text"] for b in resp.get("Blocks", []) if b["BlockType"] == "LINE"]
                        text += "\n".join(lines) + "\n"
                        print(f"[Resume] Textract page {i+1}/{num_pages}: {len(lines)} lines ({len(pdf_bytes)//1024}KB)")
                    doc.close()
                    if text.strip():
                        print(f"[Resume] Textract extracted {len(text.strip())} chars ({round((time.time()-t0_pdf)*1000)}ms)")
                    else:
                        print(f"[Resume] Textract returned 0 chars")
                except Exception as e:
                    print(f"[Resume] Textract failed: {e}")
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
async def create_session_endpoint(
    resume_text: str = Form(""),
    mode: str = Form("mock"),
    domain: str = Form("physical_design"),
    user_voice: UploadFile = File(None),
):
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

    # Store voice reference for speaker verification
    if user_voice and user_voice.filename:
        voice_bytes = await user_voice.read()
        if len(voice_bytes) > 1000:
            session["user_voice_ref"] = voice_bytes
            print(f"[Voice] Stored reference voice for session {sid} ({len(voice_bytes)} bytes)")

    sessions[sid] = session
    return {"session_id": sid, "resume": resume}


@app.post("/api/start-interview")
def start_interview(data: dict):
    _sync_runtime_config()
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

    sessions[sid] = session

    return {
        "question": greeting, "question_type": "greeting", "turn": session["turn"],
        "phase": session["phase"], "audio": audio, "difficulty": "basic",
        "should_end": False, "resume": session.get("resume", {}),
        "timing": {"tts_ms": tts_ms},
    }


@app.post("/api/transcribe")
def transcribe_endpoint(audio: UploadFile = File(...), session_id: str = Form("")):
    _sync_runtime_config()
    audio_bytes = audio.file.read()
    ext = audio.filename.rsplit(".", 1)[-1] if audio.filename else "webm"
    # Pick the STT prompt by the candidate's domain (from resume).
    session = sessions.get(session_id)
    domain = (session.get("resume", {}).get("domain", "") if session else "") or ""
    audio_duration_ms = _get_audio_duration_ms(audio_bytes, ext)
    transcript, stt_ms = transcribe_audio(audio_bytes, ext, domain=domain)
    # Store STT timing + cost in session (use actual audio duration for cost)
    stt_model = RUNTIME_CONFIG.get("stt_model", "gpt-4o-mini-transcribe")
    cost_duration = audio_duration_ms if audio_duration_ms > 0 else stt_ms  # fallback to latency
    if session:
        session.setdefault("obs_log", []).append(
            _obs_entry("STT", stt_model, stt_ms, "success" if transcript else "failure",
                       chars=len(transcript), cost_usd=_calc_stt_cost(stt_model, cost_duration)))
        # Background speaker verification
        turn = session.get("turn", 0)
        if _should_run_speaker_check(session, turn):
            threading.Thread(
                target=_verify_speaker_background,
                args=(audio_bytes, session, turn),
                daemon=True,
            ).start()
        sessions[session_id] = session

    # If speaker mismatch was flagged, inform frontend
    if session and session.get("speaker_mismatch"):
        return {"transcript": transcript, "stt_ms": stt_ms, "speaker_mismatch": True}
    return {"transcript": transcript, "stt_ms": stt_ms}


@app.post("/api/submit-answer")
def submit_answer(data: dict):
    _sync_runtime_config()
    sid = data.get("session_id")
    answer = data.get("answer", "")
    session = sessions.get(sid)
    if not session: raise HTTPException(404, "Session not found")

    # Check if speaker mismatch was detected in background
    if session.get("speaker_mismatch"):
        return {
            "question": "This interview has been ended due to a speaker verification failure.",
            "question_type": "end", "turn": session["turn"], "phase": "ended",
            "audio": "", "difficulty": "basic", "should_end": True,
            "speaker_mismatch": True,
        }

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

    sessions[sid] = session

    # Evaluate after the writeback so the eval thread's jsonb_set isn't clobbered.
    if result["should_end"]:
        _evaluate_async(session)

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
def stream_answer(data: dict):
    """SSE endpoint: LLM streams tokens → sentence buffer → TTS per sentence → audio chunks to client."""
    _sync_runtime_config()
    sid = data.get("session_id")
    answer = data.get("answer", "")
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")

    # Check if speaker mismatch was detected in background
    if session.get("speaker_mismatch"):
        def mismatch_stream():
            msg = "This interview has been ended due to a speaker verification failure."
            yield f"data: {json.dumps({'type': 'text', 'content': msg, 'done': True, 'should_end': True, 'speaker_mismatch': True})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'turn': session['turn'], 'phase': 'ended', 'speaker_mismatch': True})}\n\n"
        from starlette.responses import StreamingResponse
        return StreamingResponse(mismatch_stream(), media_type="text/event-stream")

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
            sessions[sid] = session
            _evaluate_async(session)  # hard-limit end returns early — evaluate here too
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

        for token in stream_llm(messages, temperature=0.7, max_tokens=200):
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

        # Pause prompts (e.g. "Take your time") are NOT new questions. Don't bump turn,
        # don't append to history. The candidate's next answer attaches to the ORIGINAL question.
        is_pause_prompt = _is_pause_prompt(question)
        if is_pause_prompt:
            print(f"[Stream] Pause prompt detected — not counting as a turn: \"{question}\"")
        else:
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

        # Save session to DB
        sessions[sid] = session

        # Evaluate after the writeback so the eval thread's jsonb_set isn't clobbered.
        if is_end:
            _evaluate_async(session)

        total_ms = round((time.time() - t0) * 1000)
        print(f"[Stream Turn {session['turn']}] Total: {total_ms}ms (LLM: {llm_ms}ms, {sentence_count} TTS chunks)")

        # Final done event
        yield f"data: {json.dumps({'type': 'done', 'question': question, 'turn': session['turn'], 'phase': session['phase'], 'should_end': is_end, 'pause_prompt': is_pause_prompt, 'timing': {'llm_ms': llm_ms, 'total_ms': total_ms}})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/end-session")
def end_session(data: dict):
    sid = data.get("session_id")
    session = sessions.get(sid)
    if session:
        session["phase"] = "ended"
        # Evaluate synchronously, then KEEP the session row. The admin review reads
        # from active_sessions, so deleting here would make every completed interview
        # (and its evaluation) vanish from the review. The submit/stream end paths
        # already keep ended sessions; this stays consistent with them.
        evaluate_interview(session)
        save_candidate_session(session)
        sessions[sid] = session
    return {"ok": True}


@app.get("/api/get-session")
def get_session_endpoint(session_id: str):
    session = sessions.get(session_id)
    if not session: raise HTTPException(404, "Session not found")
    return {"session_id": session_id, "phase": session["phase"], "turn": session["turn"],
            "resume": session.get("resume", {}), "mode": session.get("mode", "mock"),
            "has_voice_ref": bool(session.get("user_voice_ref"))}


@app.post("/api/generate-report")
def generate_report(data: dict):
    """Return the candidate-facing report card.

    Status values the frontend gates on:
      done     — evaluation finished, `scores` populated
      pending  — interview ended but eval still running (frontend should poll)
      skipped  — not enough answered questions to score; `scores` is the stub
                 from evaluate_interview() so the UI can still show a message
      error    — evaluation failed (LLM error, parse error, etc.)
      not_ended — interview is still in progress (shouldn't happen via normal flow)
    """
    sid = data.get("session_id") or ""
    session = sessions.get(sid)
    if not session and database.is_available():
        session = database.get_active_session(sid)
    if not session:
        raise HTTPException(404, "Session not found")

    phase = session.get("phase", "")
    evaluation = session.get("evaluation") or {}
    resume = session.get("resume", {}) or {}

    candidate = {
        "name": resume.get("candidate_name", "Candidate"),
        "domain": str(resume.get("domain", "")).replace("_", " ").title(),
        "level": str(resume.get("level", "")).replace("_", " ").title(),
        "years_experience": resume.get("years_experience", 0),
    }

    if phase != "ended":
        return {"status": "not_ended", "session_id": sid, "phase": phase,
                "turns": session.get("turn", 0), "candidate": candidate}

    status = evaluation.get("status")
    if not status:
        return {"status": "pending", "session_id": sid, "phase": phase,
                "turns": session.get("turn", 0), "candidate": candidate}

    if status == "skipped":
        return {"status": "skipped", "session_id": sid, "phase": phase,
                "turns": session.get("turn", 0), "candidate": candidate,
                "scores": {"status": "skipped",
                           "answered": evaluation.get("answered", 0),
                           "reason": evaluation.get("reason", "")}}

    if status == "error":
        return {"status": "error", "session_id": sid, "phase": phase,
                "turns": session.get("turn", 0), "candidate": candidate,
                "error": evaluation.get("error", "Evaluation failed")}

    # status == "done" — strip admin-only fields before returning to candidate.
    scores = {
        "overall_score": evaluation.get("overall_score"),
        "level_fit": evaluation.get("level_fit"),
        "verdict": evaluation.get("verdict"),
        "communication_score": evaluation.get("communication_score"),
        "communication": evaluation.get("communication"),
        "strengths": evaluation.get("strengths", []),
        "weaknesses": evaluation.get("weaknesses", []),
        "per_question": evaluation.get("per_question", []),
        "topic_breakdown": evaluation.get("topic_breakdown", []),
        "summary": evaluation.get("summary", ""),
        "answered": evaluation.get("answered", 0),
    }
    return {"status": "done", "session_id": sid, "phase": phase,
            "turns": session.get("turn", 0), "candidate": candidate,
            "scores": scores}


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
        "description": "Server-side gaze classifier (MediaPipe + RandomForest ensemble) flags when candidate looks off-screen",
        "category": "camera",
        "enabled": True,
    },
}


# ── Gaze classifier ───────────────────────────────────────────────────
# Server-side ensemble (ExtraTrees, trained on eye + iris landmarks from
# MediaPipe FaceLandmarker) classifying gaze as left / right / straight.
# Browser extracts 90 features per frame and POSTs them here for inference.
GAZE_MODEL_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "models", "gaze_ensemble_model.pkl")
_GAZE_BUNDLE      = None
_GAZE_DEFAULT_LBL = ["left", "right", "straight"]


def _load_gaze_bundle():
    global _GAZE_BUNDLE
    if _GAZE_BUNDLE is not None:
        return _GAZE_BUNDLE
    try:
        import joblib
        bundle = joblib.load(GAZE_MODEL_PATH)
        _GAZE_BUNDLE = bundle
        labels = bundle.get("label_names") or _GAZE_DEFAULT_LBL
        n_feat = len(bundle.get("feature_columns", []) or [])
        print(f"[Gaze] Loaded {os.path.basename(GAZE_MODEL_PATH)} — "
              f"model={bundle.get('name', '?')}, features={n_feat}, classes={labels}")
        return bundle
    except Exception as e:
        print(f"[Gaze] Failed to load model from {GAZE_MODEL_PATH}: {e}")
        return None

@app.post("/api/anticheat-event")
def anticheat_event(data: dict):
    event_type = data.get("event_type", "")
    # Check if this event type's feature is enabled
    feature_map = {
        "tab_switch": "tab_switch", "window_blur": "window_blur",
        "paste_event": "paste_detect", "screen_share": "screen_share",
        "dom_overlay": "dom_overlay", "canary_triggered": "canary_trigger",
        "eye_away": "eye_away",
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
        sessions[sid] = session
    return {"ok": True}

@app.get("/api/anticheat-settings")
async def anticheat_settings():
    return {k: v["enabled"] for k, v in ANTICHEAT_FEATURES.items()}


@app.post("/api/anticheat/gaze")
def anticheat_gaze(data: dict):
    """Pure gaze inference. Browser extracts the 90-float feature vector from
    MediaPipe FaceLandmarker and POSTs it here once per second. Returns the
    predicted direction plus per-class probabilities. The browser owns the
    consecutive-frames debounce; this endpoint stays stateless."""
    if not ANTICHEAT_FEATURES.get("eye_away", {}).get("enabled", False):
        return {"ok": True, "ignored": True, "reason": "feature_disabled"}
    bundle = _load_gaze_bundle()
    if not bundle:
        raise HTTPException(503, "Gaze model unavailable")
    feats = data.get("features")
    expected = len(bundle.get("feature_columns", []) or []) or 90
    if not isinstance(feats, list) or len(feats) != expected:
        raise HTTPException(400, f"Expected 'features' as a list of {expected} floats")
    try:
        import numpy as np
        x = np.asarray(feats, dtype=np.float32).reshape(1, -1)
        if not np.all(np.isfinite(x)):
            raise HTTPException(400, "Features contain NaN or inf")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Could not parse features as floats")

    model  = bundle["model"]
    labels = bundle.get("label_names") or _GAZE_DEFAULT_LBL
    t0 = time.time()
    pred_idx = int(model.predict(x)[0])
    proba = None
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba(x)[0].tolist()
    latency_ms = round((time.time() - t0) * 1000)

    out = {"ok": True, "gaze": labels[pred_idx], "latency_ms": latency_ms}
    if proba is not None:
        out["confidence"] = float(max(proba))
        out["proba"] = {labels[i]: float(p) for i, p in enumerate(proba)}
    return out

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
    _sync_runtime_config()
    return {"qgen_model": RUNTIME_CONFIG["qgen_model"], "eval_model": RUNTIME_CONFIG["eval_model"], "available_models": AVAILABLE_MODELS}

@app.post("/api/admin/llm-config")
async def set_llm_config(data: dict, _=Depends(require_admin)):
    if "qgen_model" in data: RUNTIME_CONFIG["qgen_model"] = data["qgen_model"]
    if "eval_model" in data: RUNTIME_CONFIG["eval_model"] = data["eval_model"]
    _persist_runtime_config()
    return {"status": "success", "qgen_model": RUNTIME_CONFIG["qgen_model"], "eval_model": RUNTIME_CONFIG["eval_model"]}

@app.get("/api/admin/llm-prompts")
async def get_llm_prompts(level: str = "", _=Depends(require_admin)):
    """Return the editable evaluation prompts. The admin UI shows one level at a
    time; `eval_prompt` is that level's prompt (back-compat for the single textarea)."""
    lvl = "experienced_junior" if level in ("", "fresh_graduate", "trained_fresher") else level
    if lvl not in EVAL_PROMPTS:
        lvl = "experienced_junior"
    return {
        "level": lvl,
        "levels": list(EVAL_PROMPTS.keys()),
        "eval_prompt": EVAL_PROMPTS[lvl],
        "eval_prompts": EVAL_PROMPTS,
    }

@app.post("/api/admin/llm-prompts")
async def set_llm_prompts(data: dict, _=Depends(require_admin)):
    if data.get("reset_eval"):
        EVAL_PROMPTS.update(_DEFAULT_EVAL_PROMPTS)
        return {"status": "success", "reset": True}
    if "eval_prompt" in data:
        lvl = data.get("level", "experienced_junior")
        if lvl in ("fresh_graduate", "trained_fresher"):
            lvl = "experienced_junior"
        if lvl in EVAL_PROMPTS:
            EVAL_PROMPTS[lvl] = data["eval_prompt"]
    return {"status": "success"}

@app.get("/api/admin/qgen-prompt")
async def get_qgen_prompt(domain: str = "physical_design", level: str = "experienced_junior", name: str = "Sample", _=Depends(require_admin)):
    prompt = get_interview_prompt(level, domain)
    prompt += f"\nCANDIDATE: {name} | {level.replace('_',' ')} | Tools: not specified"
    return {"prompt": prompt}


@app.get("/api/admin/interview-prompt")
async def get_interview_prompt_admin(domain: str = "physical_design", level: str = "experienced_junior", _=Depends(require_admin)):
    """Return the raw interviewer prompt file for the given domain + level.
    Used by the LLM Config viewer in the admin UI."""
    valid_domains = {"physical_design", "analog_layout", "design_verification"}
    valid_levels = {"trained_fresher", "experienced_junior", "experienced_senior", "fresh_graduate"}
    if domain not in valid_domains or level not in valid_levels:
        raise HTTPException(400, f"Invalid domain or level. Allowed domains: {valid_domains}, levels: {valid_levels}")
    # fresh_graduate and trained_fresher both route to experienced_junior prompts now.
    effective_level = "experienced_junior" if level in ("fresh_graduate", "trained_fresher") else level
    filename = f"{effective_level}_{domain}.md"
    prompt = get_interview_prompt(level, domain)
    return {"prompt": prompt, "filename": filename, "level": level, "domain": domain}


# ── Admin: Voice Config ──────────────────────────────────────────────────

@app.post("/api/toggle-tts")
async def toggle_tts(data: dict):
    RUNTIME_CONFIG["tts_enabled"] = data.get("enabled", True)
    _persist_runtime_config()
    return {"ok": True, "tts_enabled": RUNTIME_CONFIG["tts_enabled"]}

@app.get("/api/tts-status")
async def tts_status():
    _sync_runtime_config()
    return {"tts_enabled": RUNTIME_CONFIG["tts_enabled"]}

@app.get("/api/admin/stt-config")
async def get_stt_config(_=Depends(require_admin)):
    _sync_runtime_config()
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
    _persist_runtime_config()
    print(f"[STT Config] Changed to {RUNTIME_CONFIG['stt_provider']}/{RUNTIME_CONFIG['stt_model']}")
    return {"status": "success", "provider": RUNTIME_CONFIG["stt_provider"], "model": RUNTIME_CONFIG["stt_model"]}

@app.post("/api/admin/set-interview-voice")
async def set_interview_voice(data: dict, _=Depends(require_admin)):
    RUNTIME_CONFIG["tts_provider"] = data.get("provider", "deepgram")
    RUNTIME_CONFIG["tts_voice"] = data.get("voice", "")
    _persist_runtime_config()
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
def prompt_playground(data: dict, _=Depends(require_admin)):
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
def admin_sessions(_=Depends(require_admin)):
    session_list = []
    for sid, s in sessions.items():
        resume = s.get("resume", {})
        evaluation = s.get("evaluation") or {}
        overall_score = evaluation.get("overall_score")
        
        avg_score = None
        if isinstance(overall_score, (int, float)):
            avg_score = int(overall_score * 10)
            
        anticheat_log = s.get("anticheat_log", [])
        signal_count = len(anticheat_log)
        
        # Calculate trajectory dynamically if not set
        pq_by_num = {}
        for item in evaluation.get("per_question", []) or []:
            try:
                pq_by_num[int(item.get("q"))] = item
            except (TypeError, ValueError):
                continue
        scores = [pq.get("score") for pq in pq_by_num.values() if pq and isinstance(pq.get("score"), (int, float))]
        trajectory = s.get("trajectory", "stable")
        if trajectory == "unknown" or not trajectory:
            if len(scores) >= 2:
                first = sum(scores[:len(scores)//2]) / (len(scores)//2)
                last = sum(scores[len(scores)//2:]) / (len(scores) - len(scores)//2)
                if last - first > 0.8:
                    trajectory = "rising"
                elif first - last > 0.8:
                    trajectory = "falling"
                elif sum(scores)/len(scores) >= 7.5:
                    trajectory = "flat_strong"
                else:
                    trajectory = "stable"
            else:
                trajectory = "stable"
        
        session_list.append({
            "session_id": sid,
            "id": sid,
            "resume": resume,
            "phase": s.get("phase", ""),
            "turn": s.get("turn", 0),
            "mode": s.get("mode", "mock"),
            "started_at": s.get("started_at", 0),
            "difficulty_level": s.get("difficulty_level", 1),
            "candidate_name": resume.get("candidate_name", "Candidate"),
            "domain": resume.get("domain", "VLSI"),
            "level": resume.get("level", "unknown"),
            "avg_score": avg_score,
            "signal_count": signal_count,
            "trajectory": trajectory,
            "smooth_talker": s.get("smooth_talker", False),
            "anticheat_count": len(anticheat_log),
        })
    session_list.sort(key=lambda s: s.get("started_at", 0), reverse=True)
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
    for step in ["LLM_question", "LLM_greeting", "LLM_ai_detect", "LLM_evaluation", "STT", "TTS", "TTS_greeting"]:
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
def admin_session_detail(sid: str, _=Depends(require_admin)):
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    # Map per-question eval scores back onto turns. evaluate_interview numbers the
    # transcript [Q1], [Q2]... over conversation entries that have a question, so we
    # reproduce that exact counter here and look up each item by its "q" number.
    evaluation = session.get("evaluation", {}) or {}
    pq_by_num = {}
    for item in evaluation.get("per_question", []) or []:
        try:
            pq_by_num[int(item.get("q"))] = item
        except (TypeError, ValueError):
            continue

    def _get_question_topic(q: str) -> str:
        q = q.lower()
        mapping = {
            "floorplan": "Floorplanning",
            "placement": "Placement",
            "cts": "Clock Tree Synthesis",
            "clock": "Clock Tree Synthesis",
            "routing": "Routing",
            "sta": "Static Timing Analysis",
            "timing": "Timing Closure",
            "crosstalk": "Crosstalk & Noise",
            "noise": "Crosstalk & Noise",
            "ir drop": "IR Drop",
            "lvs": "LVS Checking",
            "drc": "DRC Rules",
            "synthesis": "Logic Synthesis",
        }
        for k, v in mapping.items():
            if k in q:
                return v
        return "General VLSI"

    turn_log = []
    qnum = 0
    for entry in session.get("conversation", []):
        has_q = bool((entry.get("question") or "").strip())
        if has_q:
            qnum += 1
        pq = pq_by_num.get(qnum) if has_q else None
        comment = (pq or {}).get("comment", "")
        
        # Calculate quadrant dynamically
        qd = ""
        if pq:
            score_val = pq.get("score")
            if score_val is not None:
                if score_val >= 8:
                    qd = "genuine_expert"
                elif score_val >= 5:
                    qd = "genuine_nervous"
                else:
                    qd = "dangerous_fake"
            else:
                qd = "unknown"
        
        turn = entry.get("turn", 0)
        difficulty = "basic"
        if turn >= 13:
            difficulty = "expert"
        elif turn >= 9:
            difficulty = "advanced"
        elif turn >= 5:
            difficulty = "intermediate"
        elif turn >= 2:
            difficulty = "basic"
        else:
            difficulty = "foundational"

        turn_log.append({
            "turn": turn,
            "phase": "interview",
            "question": entry.get("question", ""),
            "answer": entry.get("answer", ""),
            "question_type": "technical" if has_q else "interview",
            "topic": _get_question_topic(entry.get("question", "")) if has_q else "",
            "difficulty": difficulty,
            "score": (pq or {}).get("score", ""),
            "quality": "graded" if pq else "",
            "accuracy": "correct" if pq and pq.get("score", 0) >= 7 else "partial" if pq and pq.get("score", 0) >= 4 else "incorrect" if pq else "",
            "quadrant": qd,
            "notes": comment,
            "word_count": len((entry.get("answer") or "").split()),
            "answer_duration_sec": 0,
            "score_reasoning": comment,
            "expected_points": (pq or {}).get("expected_points", []),
            "missing_points": (pq or {}).get("missing_points", []),
            "level_gap": 0,
            "behavioral_flags": [],
            "ai_detection": entry.get("ai_detection", {}),
        })

    # Calculate trajectory
    scores = [pq.get("score") for pq in pq_by_num.values() if pq and isinstance(pq.get("score"), (int, float))]
    trajectory = "stable"
    if len(scores) >= 2:
        first = sum(scores[:len(scores)//2]) / (len(scores)//2)
        last = sum(scores[len(scores)//2:]) / (len(scores) - len(scores)//2)
        if last - first > 0.8:
            trajectory = "rising"
        elif first - last > 0.8:
            trajectory = "falling"
        elif sum(scores)/len(scores) >= 7.5:
            trajectory = "flat_strong"
        else:
            trajectory = "stable"

    return {
        "session_id": sid,
        "resume": session.get("resume", {}),
        "phase": session.get("phase", ""),
        "turn": session.get("turn", 0),
        "difficulty_level": session.get("difficulty_level", 1),
        "trajectory": trajectory,
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
        "evaluation": session.get("evaluation", {}),
        "observability": _build_session_obs(sid, session),
    }


# ── Admin: Observability ─────────────────────────────────────────────────

@app.get("/api/observability/summary")
def obs_summary(window: int = 86400, _=Depends(require_admin)):
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
    for step in ["LLM_question", "LLM_greeting", "LLM_ai_detect", "LLM_evaluation", "STT", "TTS", "TTS_greeting"]:
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
def obs_logs(limit: int = 500, _=Depends(require_admin)):
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
def submit_review(data: dict, _=Depends(require_admin)):
    return {"ok": True, "review_id": f"R-{secrets.token_hex(4).upper()}"}


# ── Start ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"  LLM: {RUNTIME_CONFIG['qgen_model']}")
    print(f"  TTS: {RUNTIME_CONFIG['tts_provider']} / {RUNTIME_CONFIG['tts_voice']}")
    print(f"  STT: gpt-4o-mini-transcribe")
    print(f"  Bedrock: {'ready' if bedrock_client else 'not configured'}")
    print(f"  Grok: {'ready' if xai_client else 'not configured'}")
    print(f"  Redis cache: {'ready' if redis_cache.is_available() else 'not configured'}")
    uvicorn.run("main:app", host="0.0.0.0", port=8001, workers=4)
