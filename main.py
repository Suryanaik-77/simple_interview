"""
Simple Interview Agent — Voice AI Mock Interview
Everything from the monolith EXCEPT: strategy engine, repetition guard,
question pipeline, evaluation pipeline, evaluation validator.

Question generation: conversation history + resume → LLM → next question.
That's it. No complex routing.
"""
import warnings
warnings.filterwarnings("ignore", module="sklearn")
import os, time, json, re, secrets, tempfile, base64, threading, smtplib, logging
from concurrent.futures import ThreadPoolExecutor
_tts_executor = ThreadPoolExecutor(max_workers=2)
from logging.handlers import RotatingFileHandler
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

# ── Logging ─────────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(
            os.path.join(_log_dir, "app.log"),
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,              # keep 5 rotated files (50 MB total max)
        ),
        logging.StreamHandler(),        # also print to stdout
    ],
)
log = logging.getLogger("interview")

# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(title="Simple Interview Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Serve static files (face-liveness bundle, etc.)
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

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

SUPPORTED_DOMAINS = {
    "physical_design": "Physical Design",
    "analog_layout": "Analog Layout",
    "design_verification": "Design Verification",
}
# LMS sends these aliases — map them to supported domains
DOMAIN_ALIASES = {
    "analog_design": "analog_layout",
}

from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

xai_client = None
if XAI_API_KEY:
    xai_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

cerebras_client = None
if CEREBRAS_API_KEY:
    cerebras_client = OpenAI(api_key=CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1")
    log.info("Cerebras LLM ready.")

bedrock_client = None
rekognition_client = None
try:
    import boto3
    bedrock_client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    rekognition_client = boto3.client("rekognition", region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
    log.info("AWS Rekognition client ready.")
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
    "cognition_model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "voice_verification_enabled": True,
}
import database
database.init_db()

import redis_cache
redis_cache.init_cache()

import comparison_analysis

# Initialize Eagle Speaker Verification (Picovoice)
try:
    import eagle_speaker_verification
    eagle_available = eagle_speaker_verification.init_eagle()
    if eagle_available:
        log.info("[Eagle] Picovoice Eagle speaker verification enabled")
    else:
        log.warning("[Eagle] Speaker verification disabled (check PICOVOICE_ACCESS_KEY)")
except Exception as e:
    log.error(f"[Eagle] Failed to initialize: {e}")
    eagle_available = False

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
        log.error(f"[History] Save failed: {e}")

# Only load the whole-file snapshot in the no-DB fallback. When the DB is the source of
# truth, returning-candidate history is fetched per-email on demand via get_candidate_previous,
# so loading every candidate into each worker at startup would be wasted RAM and stale.
candidate_history: dict[str, list[dict]] = {} if database.is_available() else _load_history()
if not database.is_available():
    log.info(f"[History] Loaded {sum(len(v) for v in candidate_history.values())} sessions for {len(candidate_history)} candidates")


def _extract_topic_from_question(question: str) -> str:
    """Extract topic from question text using keyword matching."""
    if not question:
        return ""

    q = question.lower()

    # Design Verification topics
    dv_topics = {
        "covergroup": "Functional Coverage",
        "coverage": "Functional Coverage",
        "mailbox": "Communication",
        "queue": "Data Structures",
        "scoreboard": "Verification Components",
        "interface": "Interfaces",
        "constraint": "Constrained Random",
        "random": "Constrained Random",
        "sequence": "UVM Sequences",
        "sequencer": "UVM Sequencer",
        "uvm": "UVM Framework",
        "assertion": "Assertions",
        "sva": "SystemVerilog Assertions",
        "clock domain": "Clock Domain Crossing",
        "cdc": "Clock Domain Crossing",
        "phase": "UVM Phases",
        "testbench": "Testbench Architecture",
        "fifo": "FIFO Design",
        "alu": "ALU Design",
    }

    # Physical Design topics
    pd_topics = {
        "floorplan": "Floorplanning",
        "placement": "Placement",
        "cts": "Clock Tree Synthesis",
        "clock tree": "Clock Tree Synthesis",
        "routing": "Routing",
        "sta": "Static Timing Analysis",
        "timing": "Timing Analysis",
        "setup": "Timing Constraints",
        "hold": "Timing Constraints",
        "power": "Power Optimization",
        "ir drop": "IR Drop",
        "lvs": "LVS Checking",
        "drc": "DRC Rules",
    }

    # Analog topics
    analog_topics = {
        "opamp": "Opamp Design",
        "comparator": "Comparator Design",
        "bandgap": "Bandgap Reference",
        "pll": "PLL Design",
        "adc": "ADC Design",
        "dac": "DAC Design",
    }

    # Combine all topics
    all_topics = {**dv_topics, **pd_topics, **analog_topics}

    # Find matching topic
    for keyword, topic in all_topics.items():
        if keyword in q:
            return topic

    return "General"


def save_candidate_session(session):
    """Save completed session to candidate history and generate comparison if applicable."""
    email = session.get("resume", {}).get("email", "")
    if not email:
        return

    # Extract topics from questions
    topics_asked = []
    for entry in session.get("conversation", []):
        if entry.get("question"):
            topic = _extract_topic_from_question(entry.get("question", ""))
            topics_asked.append(topic)

    summary = {
        "session_id": session["id"],
        "date": datetime.now(tz=timezone.utc).isoformat(),
        "domain": session.get("resume", {}).get("domain", ""),
        "turns": session.get("turn", 0),
        "topics_asked": topics_asked,
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
    log.info(f"[History] Saved for {email}: {summary['turns']} turns, session {summary['session_id'][:8]}")

    # Generate comparison analysis if this is not the first interview
    _generate_comparison_async(session, email)


def _generate_comparison_async(session, email):
    """Generate comparison analysis in background thread (non-blocking)."""
    def _worker():
        try:
            # Get previous sessions
            previous_sessions = get_candidate_previous(email)
            # Filter out current session
            previous_sessions = [s for s in previous_sessions if s.get("session_id") != session["id"]]

            if not previous_sessions:
                log.info(f"[Comparison] Skipping for {email} - first interview")
                return

            # Generate comparison
            comparison = comparison_analysis.compare_interviews(
                current_session=session,
                previous_sessions=previous_sessions,
                openai_client=openai_client,
                model="gpt-4o-mini"  # Use OpenAI model for comparison
            )

            # Store comparison in session data
            if database.is_available():
                database.save_session_evaluation(session["id"], {
                    **session.get("evaluation", {}),
                    "comparison": comparison
                })
            else:
                session.setdefault("evaluation", {})["comparison"] = comparison

            log.info(f"[Comparison] Generated for session {session['id'][:8]}, candidate: {email}")

        except Exception as e:
            log.error(f"[Comparison] Failed to generate async comparison: {e}")

    threading.Thread(target=_worker, daemon=True).start()


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
    "gpt-4.1-mini":                             (0.40, 1.60),
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
    "inworld/inworld-stt-1": 0.0025,  # $0.15/hr ≈ $0.0025/min
}

# TTS pricing per 1K characters (estimates)
_TTS_PRICING = {
    "deepgram": 0.015,
    "inworld": 0.015,
    "openai": 0.015,
    "kugel": 0.046,  # ~€0.043/min (Turbo) ≈ $0.046/1K chars at ~1000 chars/min English
}

# AWS Rekognition DetectFaces: $0.001/image (Group 1 API, first 1M images/month).
_REKOGNITION_COST_PER_IMAGE = 0.001


_tiktoken_enc = None
def _get_tiktoken():
    """Lazy-load tiktoken encoder (cl100k_base covers GPT-4o, GPT-4o-mini, GPT-4)."""
    global _tiktoken_enc
    if _tiktoken_enc is None:
        try:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _tiktoken_enc = False  # mark as unavailable
    return _tiktoken_enc if _tiktoken_enc else None


def _estimate_tokens(text: str, model: str = "") -> int:
    """Count tokens using tiktoken for OpenAI/Grok models, estimate for others.
    Claude ~3.5 chars/token, Llama ~3.8, OpenAI ~exact via tiktoken."""
    if not text:
        return 0
    enc = _get_tiktoken()
    # tiktoken is accurate for OpenAI and Grok (same tokenizer family)
    if enc and (not model or model.startswith("gpt-") or model.startswith("grok-")):
        return len(enc.encode(text))
    # Claude models: ~3.5 chars per token
    if model and ("anthropic" in model or "claude" in model):
        return max(1, int(len(text) / 3.5))
    # Llama/Meta models: ~3.8 chars per token
    if model and ("meta" in model or "llama" in model):
        return max(1, int(len(text) / 3.8))
    # tiktoken available but unknown model — still better than guessing
    if enc:
        return len(enc.encode(text))
    # Fallback: ~4 chars per token
    return max(1, len(text) // 4)


def _estimate_message_tokens(messages: list, model: str = "") -> int:
    """Estimate input tokens from a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content, model)
        elif isinstance(content, list):
            for block in content:
                total += _estimate_tokens(block.get("text", ""), model)
        total += 4  # role + formatting overhead
    return total


def _calc_llm_cost(model: str, input_tokens: int, output_tokens: int,
                   cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> float:
    """Calculate LLM cost in USD with prompt cache support.
    Cache reads are 90% cheaper, cache writes are 25% more expensive (Bedrock Claude)."""
    pricing = _LLM_PRICING.get(model, (0.15, 0.60))  # default to gpt-4o-mini
    in_price, out_price = pricing
    # NOTE: Anthropic/Bedrock report `input_tokens` as the UNCACHED input only —
    # cache_read_input_tokens and cache_creation_input_tokens are separate, additive
    # counts. So do NOT subtract them from input_tokens (that double-counts the
    # discount and under-reports cost). Bill each bucket at its own rate:
    #   fresh input 1x, cache reads 0.1x, cache writes 1.25x.
    cost = (input_tokens * in_price
            + cache_read_tokens * in_price * 0.1
            + cache_creation_tokens * in_price * 1.25
            + output_tokens * out_price) / 1_000_000
    return cost


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


# ── Interview Duration Tracking ──────────────────────────────────────────

def _end_interview(session, reason="manual"):
    """Mark interview as ended and track duration with reason.

    Args:
        session: Interview session dict
        reason: One of: time_limit, domain_mismatch, abusive_behavior, llm_decision,
                stop_agent, hard_max, speaker_verification_failed, manual
    """
    session["phase"] = "ended"
    session["ended_at"] = time.time()
    session["end_reason"] = reason
    started = session.get("started_at", session["ended_at"])
    duration_minutes = round((session["ended_at"] - started) / 60, 2)
    session["duration_minutes"] = duration_minutes
    sid = session.get("id", "unknown")[:8]
    log.info(f"[Interview] Session {sid} ended - Reason: {reason} | Duration: {duration_minutes} min ({session['turn']} turns)")

    # Deduct from user's lifetime quota
    email = session.get("resume", {}).get("email")
    if email and duration_minutes > 0:
        database.update_user_quota(email, duration_minutes)


# ── LLM Routing ──────────────────────────────────────────────────────────

def call_llm(messages, model_id="", temperature=0.5, max_tokens=500):
    """Route to correct LLM: OpenAI, Bedrock, or Grok.
    Returns (text, usage_dict) where usage_dict has input_tokens, output_tokens, cost_usd."""
    model = model_id or RUNTIME_CONFIG["qgen_model"]
    input_est = _estimate_message_tokens(messages, model)

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
        out_tok = getattr(resp.usage, "completion_tokens", _estimate_tokens(text, model)) if resp.usage else _estimate_tokens(text, model)
        return text, {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": _calc_llm_cost(model, in_tok, out_tok)}

    # Bedrock (Claude, Llama, Nova, etc.)
    if bedrock_client and (model.startswith("us.") or "anthropic" in model or "amazon" in model or "meta" in model):
        text, usage = _call_bedrock(messages, model, temperature, max_tokens)
        in_tok = usage.get("input_tokens") or input_est
        out_tok = usage.get("output_tokens") or _estimate_tokens(text, model)
        cr = usage.get("cache_read_input_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        return text, {"input_tokens": in_tok, "output_tokens": out_tok,
                       "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc,
                       "cost_usd": _calc_llm_cost(model, in_tok, out_tok, cr, cc)}

    # OpenAI (default)
    resp = openai_client.chat.completions.create(
        model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    text = resp.choices[0].message.content.strip()
    in_tok = resp.usage.prompt_tokens if resp.usage else input_est
    out_tok = resp.usage.completion_tokens if resp.usage else _estimate_tokens(text, model)
    return text, {"input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": _calc_llm_cost(model, in_tok, out_tok)}


def stream_llm(messages, model_id="", temperature=0.5, max_tokens=500):
    """Stream LLM tokens. Yields text chunks. Works with OpenAI and Grok."""
    import httpx
    model = model_id or RUNTIME_CONFIG["qgen_model"]

    # Grok streaming
    if model.startswith("grok-") and xai_client:
        try:
            stream = xai_client.chat.completions.create(
                model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens,
                stream=True, timeout=httpx.Timeout(20.0),
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            log.error(f"[StreamLLM] Grok streaming error: {e}")
        return

    # Bedrock streaming (Claude, Llama, Nova, etc.)
    if bedrock_client and (model.startswith("us.") or "anthropic" in model or "amazon" in model or "meta" in model):
        try:
            for chunk in _stream_bedrock(messages, model, temperature, max_tokens):
                yield chunk
        except Exception as e:
            log.error(f"[StreamLLM] Bedrock streaming error: {e}")
        return

    # OpenAI streaming
    try:
        stream = openai_client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True, timeout=httpx.Timeout(20.0),
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
    except Exception as e:
        log.error(f"[StreamLLM] OpenAI streaming error: {e}")


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
        log.error(f"[TTS] Kugel error: {e}")
        return b""


def tts_chunk(text: str) -> bytes:
    """Generate TTS audio bytes for a text chunk. Returns raw audio bytes."""
    if not RUNTIME_CONFIG.get("tts_enabled", True) or not text.strip():
        return b""
    # Underscores are kept in the UI (e.g. set_false_path) but read oddly aloud,
    # so replace them with spaces for TTS only.
    text = text.replace("_", " ")
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
            log.error(f"[TTS Stream] Deepgram error: {e}")

    if provider == "kugel" and KUGEL_API_KEY:
        wav = _kugel_tts(text[:2000], voice)
        if wav:
            return wav

    if provider == "inworld" and INWORLD_API_KEY:
        iw_body = {"text": text[:2000], "voiceId": voice or INWORLD_VOICE_ID, "modelId": INWORLD_MODEL_ID,
                   "audioConfig": {"speakingRate": 1.25}}
        try:
            r = http_requests.post("https://api.inworld.ai/tts/v1/voice:stream",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json=iw_body, timeout=15, stream=True)
            r.raise_for_status()
            audio_parts = []
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    ac = chunk.get("result", chunk).get("audioContent")
                    if ac:
                        audio_parts.append(base64.b64decode(ac))
                except (json.JSONDecodeError, KeyError):
                    pass
            if audio_parts:
                return b"".join(audio_parts)
        except Exception as e:
            log.error(f"[TTS Stream] Inworld stream error: {e}")
        try:
            r = http_requests.post("https://api.inworld.ai/tts/v1/voice",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json=iw_body, timeout=15)
            r.raise_for_status()
            data = r.json() if "json" in r.headers.get("content-type", "") else None
            if data and data.get("audioContent"):
                return base64.b64decode(data["audioContent"])
            return r.content
        except Exception as e:
            log.error(f"[TTS Stream] Inworld error: {e}")

    _OPENAI_VOICES = {"nova", "shimmer", "echo", "onyx", "fable", "alloy", "ash", "sage", "coral"}
    if OPENAI_API_KEY:
        try:
            oai_voice = voice if voice in _OPENAI_VOICES else "nova"
            response = openai_client.audio.speech.create(model="tts-1", voice=oai_voice, input=text[:2000])
            return response.content
        except Exception as e:
            log.error(f"[TTS Stream] OpenAI error: {e}")

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
        # Prompt caching: cache ONLY the stable system block (set below). We do NOT
        # put a breakpoint on the last user turn: the conversation history grows and
        # the volatile per-turn steering (asked-ledger / project coverage) is merged
        # into that turn, so it differs every request — caching it just writes a fresh
        # cache each turn that the next turn can never read (all writes, no reads).
        # The system block, by contrast, is byte-identical for the whole session, so a
        # single cache_control there gives a read hit on every turn after the first.
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
    """Call AWS Bedrock models (non-streaming). Returns (text, usage_dict)."""
    body, model_type = _build_bedrock_body(messages, model_id, temperature, max_tokens)
    resp = bedrock_client.invoke_model(modelId=model_id, contentType="application/json", accept="application/json", body=json.dumps(body))
    result_body = json.loads(resp["body"].read())
    text = _parse_bedrock_response(result_body, model_type)
    usage = result_body.get("usage", {})
    return text, {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
    }


_last_stream_bedrock_usage = {}

def _stream_bedrock(messages, model_id, temperature, max_tokens):
    """Stream tokens from AWS Bedrock. Yields text chunks.
    Supports Claude (content_block_delta), Nova (contentBlockDelta),
    and Llama (generation token). Falls back to non-streaming for unknown models.
    After iteration, usage is available in _last_stream_bedrock_usage."""
    global _last_stream_bedrock_usage
    _last_stream_bedrock_usage = {}
    body, model_type = _build_bedrock_body(messages, model_id, temperature, max_tokens)

    try:
        resp = bedrock_client.invoke_model_with_response_stream(
            modelId=model_id, contentType="application/json", accept="application/json",
            body=json.dumps(body))
        stream = resp.get("body")
        if not stream:
            full, usage = _call_bedrock(messages, model_id, temperature, max_tokens)
            _last_stream_bedrock_usage = usage
            yield full
            return

        for event in stream:
            chunk = event.get("chunk")
            if not chunk:
                continue
            payload = json.loads(chunk["bytes"])

            if model_type == "claude":
                if payload.get("type") == "content_block_delta":
                    text = payload.get("delta", {}).get("text", "")
                    if text:
                        yield text
                elif payload.get("type") == "message_delta":
                    usage = payload.get("usage", {})
                    _last_stream_bedrock_usage["output_tokens"] = usage.get("output_tokens", 0)
                elif payload.get("type") == "message_start":
                    usage = payload.get("message", {}).get("usage", {})
                    _last_stream_bedrock_usage.update({
                        "input_tokens": usage.get("input_tokens", 0),
                        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                    })
            elif model_type == "nova":
                delta = payload.get("contentBlockDelta", {}).get("delta", {})
                text = delta.get("text", "")
                if text:
                    yield text
            elif model_type == "llama":
                text = payload.get("generation", "")
                if text:
                    yield text
            else:
                text = (payload.get("delta", {}).get("text", "") or
                        payload.get("generation", "") or
                        payload.get("outputText", ""))
                if text:
                    yield text

    except Exception as e:
        log.error(f"[Bedrock Stream] Streaming failed ({e}), falling back to non-streaming")
        full, usage = _call_bedrock(messages, model_id, temperature, max_tokens)
        _last_stream_bedrock_usage = usage
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
            log.error(f"[Cerebras] Failed, falling back: {e}")
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


def _is_repeat_request(text: str) -> bool:
    """True if the candidate is asking to repeat the question.
    These should NOT be stored as answers — we should repeat the question."""
    if not text:
        return False
    t = text.strip().lower().strip("\"'").rstrip(".!?,").strip()
    if not t:
        return False

    # Common phrases asking to repeat
    _REPEAT_PHRASES = {
        "repeat", "repeat the question", "repeat question", "repeat that",
        "say again", "say that again", "can you repeat", "could you repeat",
        "please repeat", "pardon", "sorry", "what", "excuse me",
        "didn't understand", "didnt understand", "didn't get that", "didnt get that",
        "can you say that again", "could you say that again",
        "i didn't understand", "i didnt understand",
        "i didn't get that", "i didnt get that",
        "what was the question", "what is the question",
        "come again", "huh", "eh"
    }

    if t in _REPEAT_PHRASES:
        return True

    # Partial matches for flexibility
    if any(phrase in t for phrase in ["repeat", "say again", "didn't understand", "didnt understand", "pardon"]):
        if len(t) < 50:  # Short utterances only
            return True

    return False


def _is_pause_prompt(text: str) -> bool:
    """True if the LLM output is a 'Take your time' / 'Go ahead' style pause prompt.
    These don't count as new questions — the candidate's next answer must attach
    to the ORIGINAL question, not to this prompt."""
    if not text:
        return False
    t = text.strip().lower().strip("\"'").rstrip(".!?,").strip()
    if not t:
        return False
    _PAUSE_EXACT = {
        "take your time", "please take your time", "ok take your time",
        "alright take your time", "sure take your time", "no rush",
        "take a moment", "please take a moment",
        "go ahead", "go ahead finish that thought",
        "go ahead complete your thought", "go ahead complete your thoughts",
        "please go ahead", "sure go ahead",
        "finish that thought", "complete your thought", "complete your thoughts",
        "go on", "please continue", "continue",
        "i'm listening", "im listening",
        "whenever you're ready", "whenever you are ready",
        "no worries take your time", "no worries",
    }
    if t in _PAUSE_EXACT:
        return True
    if len(t) > 80:
        return False
    _PAUSE_FRAGMENTS = ("take your time", "finish that thought", "complete your thought",
                        "complete your thoughts", "whenever you're ready",
                        "whenever you are ready", "i'm listening", "im listening")
    if any(f in t for f in _PAUSE_FRAGMENTS):
        return True
    # "go ahead" only counts as pause if the utterance is short (not a real question
    # like "Go ahead and explain clock tree synthesis")
    return len(t) <= 50 and "go ahead" in t


# ── Resume Parsing ───────────────────────────────────────────────────────

def parse_resume(resume_text: str) -> dict:
    if not resume_text or len(resume_text.strip()) < 20:
        return {}
    today_str = datetime.now().strftime("%B %Y")
    prompt = f"""Extract from this resume. Return ONLY valid JSON:
{{"candidate_name":"","email":"","phone":"","level":"fresh_graduate|trained_fresher|experienced_junior|experienced_senior",
"years_experience":0,"skills":[],"tools":[],"key_projects":[{{"name":"project name","description":"1-2 sentence summary of what was done, role, node, challenges"}}],"domain":"","education":""}}

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
- key_projects: list of objects with "name" and "description" (1-2 sentence summary including role, technology node, key challenges). Extract ALL projects.
- domain: the candidate's PRIMARY VLSI specialization. Choose exactly one, deciding from the TOOLS and PROJECTS as a whole (not a single keyword):
    analog_layout — hand-drawn, transistor-level custom IC layout in Cadence Virtuoso:
      device matching & symmetry, op-amps, LDOs, bandgap references, PLLs, VCOs, ADCs,
      current mirrors, shielding, parasitic-aware layout, ESD / latch-up, EM/IR at the
      device level. Tools: Virtuoso, Layout XL/Suite, Assura, PVS (Calibre for DRC/LVS).
      IMPORTANT: analog-layout engineers also say "standard cell layout" and "DRC/LVS
      clean" — those phrases ALONE do NOT make it physical_design. If the work is
      DRAWING layout by hand in Virtuoso, it is analog_layout.
    physical_design — digital RTL-to-GDSII implementation: synthesis, floorplan,
      placement, CTS, routing, STA / setup-hold timing closure, power planning, IR/EM
      signoff, ECO, congestion. Tools: Innovus, ICC2, Fusion Compiler, PrimeTime, Tempus.
    design_verification — functional verification: SystemVerilog/UVM testbenches,
      assertions, functional coverage, simulation/regressions. Tools: VCS, Questa, Xcelium.
  Rule of thumb: Virtuoso + op-amp/LDO/PLL/VCO layout + device matching ⇒ analog_layout;
  Innovus/ICC2 + timing closure/ECO ⇒ physical_design; UVM/testbench/coverage ⇒
  design_verification. If genuinely ambiguous, leave "".

RESUME:
{resume_text[:3000]}

JSON:"""
    for attempt in range(3):
        try:
            raw = call_cerebras([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=800)
            parsed = safe_json(raw)
            if parsed and parsed.get("candidate_name"):
                log.info(f"[Resume] Parsed on attempt {attempt+1}: {parsed.get('candidate_name')}")
                return parsed
            log.info(f"[Resume] Attempt {attempt+1}: empty result, retrying...")
        except Exception as e:
            log.error(f"[Resume] Attempt {attempt+1} failed: {e}")
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
            log.info(f"[STT] Loaded domain prompt: {domain} ({len(text)} chars)")
            return text
    except FileNotFoundError:
        log.info(f"[STT] No prompt file for domain '{domain}' — using generic")
    except Exception as e:
        log.error(f"[STT] Failed to load prompt for '{domain}': {e}")
    _STT_PROMPT_CACHE[domain] = _OPENAI_STT_PROMPT
    return _OPENAI_STT_PROMPT


_STT_HALLUCINATION_RE = re.compile(
    r"^(thank you( for watching| for listening)?\.?|thanks for watching\.?"
    r"|please subscribe\.?|you|\.+|,+|\s+)$",
    re.IGNORECASE,
)

def _is_stt_hallucination(text: str) -> bool:
    if not text:
        return False
    if "This is a VLSI" in text or "VLSI semiconductor" in text or "Common terms:" in text:
        return True
    if _STT_HALLUCINATION_RE.match(text.strip()):
        return True
    return False

def transcribe_audio(audio_bytes: bytes, ext: str = "webm", domain: str = "") -> tuple[str, int]:
    """Returns (transcript, latency_ms)."""
    provider = RUNTIME_CONFIG.get("stt_provider", "openai")
    model = RUNTIME_CONFIG.get("stt_model", "gpt-4o-mini-transcribe")
    tmp_path = None
    t0 = time.time()

    # Inworld STT
    if provider == "inworld" and INWORLD_API_KEY:
        try:
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            # Map common audio extensions to Inworld encoding names
            enc_map = {"webm": "AUTO_DETECT", "wav": "LINEAR16", "mp3": "MP3", "ogg": "OGG_OPUS", "flac": "FLAC"}
            encoding = enc_map.get(ext, "AUTO_DETECT")
            r = http_requests.post("https://api.inworld.ai/stt/v1/transcribe",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json={"model": model if model.startswith("inworld/") else "inworld/inworld-stt-1",
                      "audioEncoding": encoding, "sampleRateHertz": 16000, "audio": audio_b64},
                timeout=15)
            r.raise_for_status()
            data = r.json()
            text = data.get("transcript", data.get("text", "")).strip()
            latency = round((time.time() - t0) * 1000)
            if _is_stt_hallucination(text):
                log.warning(f"[STT] Inworld/{model} {latency}ms — HALLUCINATION filtered: \"{text}\"")
                return "", latency
            log.info(f"[STT] Inworld/{model} {latency}ms — {len(text)} chars")
            return text, latency
        except Exception as e:
            log.error(f"[STT] Inworld error: {e}, falling back to OpenAI")

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
            if _is_stt_hallucination(text):
                log.warning(f"[STT] Deepgram/{model} {latency}ms — HALLUCINATION filtered: \"{text}\"")
                return "", latency
            log.info(f"[STT] Deepgram/{model} {latency}ms — {len(text)} chars")
            return text, latency
        except Exception as e:
            log.error(f"[STT] Deepgram error: {e}, falling back to OpenAI")

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
        if _is_stt_hallucination(text):
            log.warning(f"[STT] OpenAI/{model} {latency}ms — HALLUCINATION filtered: \"{text}\"")
            return "", latency
        log.info(f"[STT] OpenAI/{model} {latency}ms — {len(text)} chars (domain={domain or 'generic'})")
        return text, latency
    except Exception as e:
        log.error(f"[STT] Error: {e}")
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
    log.info(f"[WS] Audio stream connected (session={sid}, vad=browser)")

    try:
        while True:
            await ws.receive_bytes()
    except WebSocketDisconnect:
        log.info(f"[WS] Audio stream disconnected (session={sid})")
    except Exception as e:
        log.error(f"[WS] Error: {e}")


# ── TTS ──────────────────────────────────────────────────────────────────

def synthesize_speech(text: str) -> tuple[str, int]:
    """Returns (base64_audio, latency_ms)."""
    if not RUNTIME_CONFIG.get("tts_enabled", True) or not text:
        return "", 0
    # Underscores read oddly aloud; strip them for TTS only (UI keeps them).
    text = text.replace("_", " ")
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
            log.info(f"[TTS] Deepgram {latency}ms — {len(text)} chars")
            return base64.b64encode(r.content).decode(), latency
        except Exception as e:
            log.error(f"[TTS] Deepgram error: {e}")

    # Kugel
    if provider == "kugel" and KUGEL_API_KEY:
        wav = _kugel_tts(text[:2000], voice)
        latency = round((time.time() - t0) * 1000)
        if wav:
            log.info(f"[TTS] Kugel {latency}ms — {len(text)} chars (voice={voice})")
            return base64.b64encode(wav).decode(), latency

    # Inworld
    if provider == "inworld" and INWORLD_API_KEY:
        try:
            r = http_requests.post("https://api.inworld.ai/tts/v1/voice",
                headers={"Authorization": f"Basic {INWORLD_API_KEY}", "Content-Type": "application/json"},
                json={"text": text[:2000], "voiceId": voice or INWORLD_VOICE_ID, "modelId": INWORLD_MODEL_ID,
                      "audioConfig": {"speakingRate": 1.25}}, timeout=15)
            r.raise_for_status()
            latency = round((time.time() - t0) * 1000)
            log.info(f"[TTS] Inworld {latency}ms — {len(text)} chars")
            data = r.json() if "json" in r.headers.get("content-type", "") else None
            if data: return data.get("audioContent", base64.b64encode(r.content).decode()), latency
            return base64.b64encode(r.content).decode(), latency
        except Exception as e:
            log.error(f"[TTS] Inworld error: {e}")

    # OpenAI TTS (fallback — use valid OpenAI voice)
    _OPENAI_VOICES = {"nova", "shimmer", "echo", "onyx", "fable", "alloy", "ash", "sage", "coral"}
    if OPENAI_API_KEY:
        try:
            oai_voice = voice if voice in _OPENAI_VOICES else "nova"
            response = openai_client.audio.speech.create(model="tts-1", voice=oai_voice, input=text[:2000])
            latency = round((time.time() - t0) * 1000)
            log.info(f"[TTS] OpenAI {latency}ms — {len(text)} chars")
            return base64.b64encode(response.content).decode(), latency
        except Exception as e:
            log.error(f"[TTS] OpenAI error: {e}")

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
        log.info(f"[Prompt] Loaded {filename}")
        return prompt
    except FileNotFoundError:
        log.warning(f"[Prompt] {filename} not found, falling back to experienced_junior_physical_design.md")
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


def _norm_expected_points(ep):
    """Normalize expected_points to a list of (text, weight) tuples. Tolerates the
    old string format (treated as 'core') and the new weighted-object format."""
    out = []
    for p in ep or []:
        if isinstance(p, dict):
            t = str(p.get("point", "")).strip()
            w = str(p.get("weight", "core")).lower()
            w = "core" if w not in ("core", "extra") else w
        elif isinstance(p, str):
            t, w = p.strip(), "core"
        else:
            continue
        if t:
            out.append((t, w))
    return out


def generate_expected_points(question: str, domain: str, level: str, session: dict):
    """Background LLM call to generate expected key points for a question, each tagged
    with a weight (core = essential, extra = nice-to-have). Stored on the conversation
    entry so the next turn can (a) steer follow-ups toward MISSING CORE points only and
    (b) give the evaluator an explicit importance-weighted reference set."""
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
                  f"For the interview question, list 3-5 KEY POINTS expected in a good answer "
                  f"from a {level.replace('_', ' ')} candidate. Tag each point's weight: "
                  f"\"core\" = essential to show real understanding (1-3 of these), "
                  f"\"extra\" = a nice-to-have detail. Each point is 1 short sentence (under 15 words). "
                  f"Return ONLY a valid JSON array of objects, no markdown. "
                  f"Example: [{{\"point\":\"...\",\"weight\":\"core\"}},{{\"point\":\"...\",\"weight\":\"extra\"}}]")
    user_msg = f'Question: "{question}"'

    try:
        t0 = time.time()
        raw, usage = call_llm([{"role": "system", "content": system_msg},
                               {"role": "user", "content": user_msg}],
                              temperature=0.0, max_tokens=400)
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
        obs = _obs_entry("LLM_expected_points", RUNTIME_CONFIG["qgen_model"], ms, "success",
                         input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                         cost_usd=usage["cost_usd"])
        conv = session.get("conversation", [])
        if isinstance(points, list) and points:
            # Locate the turn by scanning from the end (positional index — do NOT use
            # list.index(), which matches by value and can hit an earlier duplicate turn).
            turn_idx = next((i for i in range(len(conv) - 1, -1, -1)
                             if conv[i].get("question") == question), None)
            if turn_idx is not None:
                conv[turn_idx]["expected_points"] = points
                # Atomic DB patch — avoids full read-modify-write race with the session blob.
                database.update_session_expected_points(session["id"], turn_idx, points)
            log.info(f"[ExpectedPts] {ms}ms | {len(points)} points | ${usage['cost_usd']:.4f}")
        # Persist cost as an atomic array-append (not an in-memory append that a later
        # full-save would double-count), then invalidate the stale cache so the next
        # read repopulates from Postgres — which now holds the patch.
        database.append_session_obs_log(session["id"], obs)
        redis_cache.delete_session(session["id"])
    except Exception as e:
        log.error(f"[ExpectedPts] Failed: {e}")


def classify_question_tags(session, question: str):
    """Background agent that labels a freshly-asked question by comparing it with the
    PREVIOUS question and the candidate's ANSWER to it. It decides:
      - is_followup: does this continue the same thread, or open a new topic?
      - is_scenario: is it a hypothetical debug/what-if the candidate didn't describe?
      - qtype: PROJECT / CONCEPT / SCENARIO (feeds the 40/40/20 mix steering).
    Runs OFF the hot path (like expected-points) so it never delays the question the
    candidate hears. Writes the flags onto the conversation entry and persists them,
    so the next turn's steering can read them."""
    if not question or _is_pause_prompt(question):
        return
    conv = session.get("conversation", [])
    idx = next((i for i in range(len(conv) - 1, -1, -1)
                if conv[i].get("question") == question), None)
    if idx is None:
        return
    # Never classify the opening greeting itself.
    if conv[idx].get("is_greeting"):
        return
    prev = conv[idx - 1] if idx > 0 else None
    prev_q = (prev or {}).get("question", "")
    prev_a = (prev or {}).get("answer", "")
    prev_was_followup = bool((prev or {}).get("is_followup"))
    # The previous turn being the greeting/intro means THIS is the first real
    # technical question — it can never be a follow-up of an intro. Keyed off the
    # explicit is_greeting flag, not the greeting wording (which varies).
    prev_is_intro = (not prev_q) or bool((prev or {}).get("is_greeting"))

    system_msg = (
        "You label one interview question. Compare the NEW question with the PREVIOUS "
        "question and the candidate's ANSWER to it. Return ONLY JSON: "
        '{"followup": true/false, "scenario": true/false, "type": "PROJECT|CONCEPT|SCENARIO"}.\n'
        "followup=true if the NEW question stays on the SAME topic/thread — drilling the "
        "previous answer, asking for a missing detail, or reacting to something they just "
        "said; false if it opens a NEW topic.\n"
        "scenario=true if the NEW question is a hypothetical symptom / what-if the candidate "
        "did NOT already describe and must reason through.\n"
        "type=PROJECT if it is about their own work/projects; CONCEPT if it is a clean "
        "standalone fundamentals question; SCENARIO if it is a hypothetical.")
    user_msg = (f"PREVIOUS question: {prev_q or '(none — first question)'}\n"
                f"Candidate ANSWER: {prev_a or '(none)'}\n"
                f"NEW question: {question}")
    try:
        t0 = time.time()
        raw, usage = call_llm([{"role": "system", "content": system_msg},
                               {"role": "user", "content": user_msg}],
                              temperature=0.0, max_tokens=60)
        ms = round((time.time() - t0) * 1000)
        data = safe_json(raw) or {}
        is_followup = bool(data.get("followup"))
        is_scenario = bool(data.get("scenario"))
        qtype = str(data.get("type", "")).upper()
        if qtype not in ("PROJECT", "CONCEPT", "SCENARIO"):
            qtype = "SCENARIO" if is_scenario else "CONCEPT"
        # Keep the scenario flag consistent with the type verdict.
        if qtype == "SCENARIO":
            is_scenario = True
        # A first technical question is never a follow-up of the intro.
        if prev_is_intro:
            is_followup = False
        # Never chain two follow-ups in a row (deterministic backstop).
        if is_followup and prev_was_followup:
            is_followup = False
        entry = conv[idx]
        obs = _obs_entry("LLM_tag_classify", RUNTIME_CONFIG["qgen_model"], ms, "success",
                         input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                         cost_usd=usage["cost_usd"])
        entry["is_followup"] = is_followup
        entry["is_scenario"] = is_scenario
        entry["qtype"] = qtype
        # Atomic DB patches — avoid full read-modify-write race with the session blob.
        # Persist the cost as an array-append (not an in-memory append a later full-save
        # would double-count), then invalidate the stale cache so the next read
        # repopulates from Postgres — which now holds these patches.
        database.update_session_question_tags(session["id"], idx, is_followup, is_scenario, qtype)
        database.append_session_obs_log(session["id"], obs)
        redis_cache.delete_session(session["id"])
        log.info(f"[TagClassify] {ms}ms followup={is_followup} scenario={is_scenario} type={qtype}")
    except Exception as e:
        log.error(f"[TagClassify] Failed: {e}")



# ── Curated question bank (from seed_question_bank.py) ─────────────────────────────
# Research on LLM interviewers is clear: a curated question set calibrates an
# interview better than turning the model loose to invent probes (which tunnels
# and skips the easy/medium rungs). We already ship one — seed_question_bank —
# but it was only used to seed the DB, never fed to the live interviewer. Load it
# once, grouped by domain -> difficulty, so we can hand the LLM real, level-
# appropriate CONCEPT questions to open easy and calibrate against.
def _load_question_bank():
    try:
        from seed_question_bank import QUESTIONS
    except Exception as e:
        log.warning(f"[Bank] seed_question_bank unavailable: {e}")
        return {}
    bank = {}
    for q in QUESTIONS:
        d, diff, txt = q.get("domain"), q.get("difficulty"), q.get("question_text")
        if d and diff and txt:
            bank.setdefault(d, {}).setdefault(diff, []).append(txt)
    n = sum(len(v) for t in bank.values() for v in t.values())
    log.info(f"[Bank] loaded {n} curated questions across {len(bank)} domains")
    return bank

QUESTION_BANK = _load_question_bank()

# Per-domain job descriptions — these scope the interview (concept/scenario
# topics come from the role's JD, not the candidate's self-listed skills).
try:
    from job_descriptions import render_job_description
except Exception as _jd_e:  # keep the app running even if the JD file is missing
    log.warning(f"[JD] job_descriptions unavailable ({_jd_e}) — falling back to résumé scope")
    def render_job_description(domain):
        return ""


def _bank_block_for(domain, level, session, session_index=0):
    """A small, level-appropriate sample of curated questions as difficulty/style
    exemplars.

    Non-repeating per user: the pool is shuffled deterministically by the CANDIDATE
    (email), then we take a window offset by session_index (how many interviews this
    user already had). So session 1 gets a different slice than session 2, etc. — a
    returning candidate never sees the same exemplars twice until the pool is
    exhausted. Stable within a session (session_index doesn't change mid-interview),
    so the prompt still caches."""
    import random, hashlib
    tiers = QUESTION_BANK.get(domain, {})
    if not tiers:
        return ""
    basic = list(tiers.get("basic", []))
    inter = list(tiers.get("intermediate", []))
    adv = list(tiers.get("advanced", []))
    # Seed by the user (email), so each person has their own stable ordering; fall
    # back to session id for anonymous sessions.
    user_key = (session.get("resume", {}).get("email") or str(session.get("id", ""))).lower()
    seed = int(hashlib.md5(user_key.encode()).hexdigest(), 16) & 0xffffffff
    rng = random.Random(seed)
    rng.shuffle(basic); rng.shuffle(inter); rng.shuffle(adv)

    def window(pool, k, idx):
        # rotate the (per-user stable) pool by idx*k so consecutive sessions take
        # disjoint slices; wraps around once the pool is used up.
        if not pool:
            return []
        off = (idx * k) % len(pool)
        return (pool[off:] + pool[:off])[:k]

    junior = level in ("fresh_graduate", "trained_fresher", "experienced_junior")
    picks = [("EASY", window(basic, 7, session_index)), ("MEDIUM", window(inter, 5, session_index))] if junior \
        else [("MEDIUM", window(inter, 6, session_index)), ("HARD", window(adv, 6, session_index))]
    lines = [f"- ({label}) {q}" for label, qs in picks for q in qs]
    if not lines:
        return ""
    header = (
        "\n\nDOMAIN QUESTION BANK — real interview questions at this candidate's level, "
        "easy first. Use them to calibrate the DIFFICULTY and PHRASING of your CONCEPT "
        "questions and to open easy. Adapt or rephrase to the candidate, stay within the "
        "topics on their résumé, and you may ask one close to verbatim when it fits. These "
        "are standalone concept checks — never bolt them onto a project.\n")
    return header + "\n".join(lines)


def build_interview_prompt(session):
    """Build prompt by loading the right file for level + domain, then appending candidate info."""
    resume = session.get("resume", {})
    history = session.get("conversation", [])

    name = resume.get("candidate_name", "Candidate")
    level = resume.get("level", "trained_fresher")
    domain = resume.get("domain", "physical_design")
    years = resume.get("years_experience", 0)
    # NOTE: tools/skills are intentionally not read into the prompt here — they are
    # for on-page display only. Only projects (below) reach the interviewer.

    # Format projects with descriptions if available
    raw_projects = resume.get("key_projects", [])
    if raw_projects and isinstance(raw_projects[0], dict):
        proj_lines = []
        for p in raw_projects:
            pname = p.get("name", "")
            pdesc = p.get("description", "")
            proj_lines.append(f"  - {pname}: {pdesc}" if pdesc else f"  - {pname}")
        projects_str = "\n".join(proj_lines)
    else:
        projects_str = ", ".join(str(p) for p in raw_projects) if raw_projects else "not specified"

    # Load self-contained prompt for this level + domain
    base_prompt = get_interview_prompt(level, domain)
    # Only the candidate's PROJECTS (and their descriptions) reach the prompt.
    # Tools and skills are deliberately NOT sent to the interviewer — they exist
    # only for display on the interview page. The interview's topic scope comes
    # from the JOB DESCRIPTION (below); projects drive PROJECT-type questions.
    if "\n" in projects_str:
        candidate_info = f"\nCANDIDATE: {name} | {level.replace('_',' ')} | {years} years\nProjects:\n{projects_str}"
    else:
        candidate_info = f"\nCANDIDATE: {name} | {level.replace('_',' ')} | {years} years | Projects: {projects_str}"

    # Check for returning candidate
    returning_block = ""
    session_index = 0  # how many interviews this candidate already had (0 = first)
    email = resume.get("email", "")
    if email:
        prev_sessions = get_candidate_previous(email)
        session_index = len(prev_sessions)
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
                    prev_projects.add(p.get("name", str(p)) if isinstance(p, dict) else str(p))

            projects_note = ""
            if prev_projects:
                projects_note = f"\nProjects discussed before: {', '.join(prev_projects)}\nAsk about DIFFERENT aspects of these projects, or explore projects not yet discussed."

            returning_block = f"""
RETURNING CANDIDATE: This candidate has interviewed {len(prev_sessions)} time(s) before.
These questions were already asked in previous sessions:
{chr(10).join(f'- {q}' for q in prev_questions)}{projects_note}
This is a completely NEW interview. Ask fresh questions from different angles on the same topics.
Test whether the candidate has genuinely improved or just memorized answers from before."""

    # ── Anti-repetition ledger ────────────────────────────────────────────
    # The Q&A history below is intentionally trimmed to the last 2 turns (focused
    # context for a follow-up), so everything earlier is out of the model's view.
    # This ledger lists EVERY question asked this session so the model always knows
    # the full set it has already covered and cannot re-ask or reword one — the #1
    # source of duplicate questions.
    asked = [e["question"] for e in history
             if e.get("question") and not _is_pause_prompt(e["question"])
             and not e.get("is_greeting")]
    asked_block = ""
    if asked:
        asked_lines = "\n".join(f"- {q}" for q in asked)
        asked_block = (
            "\n\nQUESTIONS ALREADY ASKED THIS SESSION — this is the COMPLETE list. Do NOT "
            "ask any of these again, and do NOT ask a reworded or rephrased version of the "
            "SAME topic (e.g. 'what is a guard ring' and 'what are guard rings' are the same "
            "question — never both). CRITICAL: If you already asked about scoreboard implementation, "
            "do NOT ask it again even in a different context (ALU vs UVM vs general). If you asked "
            "about interface usage, do NOT ask about interface advantages. One question per core concept. "
            "You may drill deeper on the candidate's MOST RECENT answer, but never re-open an earlier "
            "topic you already moved on from. Every new top-level question MUST be about a topic, "
            "project, tool, or concept that does NOT appear anywhere in this list:\n{asked_lines}")

    # ── Resume-project rotation ───────────────────────────────────────────
    # Data-driven balance using the candidate's OWN project names (not a
    # hardcoded topic taxonomy): count how many questions already touched each
    # project and steer the next one toward an under-covered project so no
    # single project dominates the interview.
    proj_names = []
    for p in raw_projects:
        nm = (p.get("name", "") if isinstance(p, dict) else str(p)).strip()
        if nm:
            proj_names.append(nm)

    def _mentions(q, name):
        ql, nl = q.lower(), name.lower()
        if nl in ql:
            return True
        first = name.split()[0].lower() if name.split() else ""
        return len(first) >= 4 and first in ql

    project_block = ""
    if proj_names and asked:
        counts = {nm: sum(1 for q in asked if _mentions(q, nm)) for nm in proj_names}
        if max(counts.values()) >= 1:  # only steer once a project has been covered
            cov = "; ".join(f"{nm} ({counts[nm]})" for nm in proj_names)
            fewest = min(counts.values())
            under = [nm for nm in proj_names if counts[nm] == fewest]
            project_block = (
                f"\n\nPROJECT COVERAGE so far — {cov}. Rotate across the candidate's projects: "
                f"when your next question is project-anchored (PROJECT or SCENARIO), aim it at "
                f"an UNDER-covered project ({', '.join(under)}). Do not keep adding questions "
                "to the most-covered project while others are thin. This rotation does NOT "
                "apply to CONCEPT checks — those are standalone and never name a project, so "
                "never bend a concept question toward a project to satisfy rotation.")

    # ── Follow-up vs move-on, and theme balance ───────────────────────────
    # Both are the interviewer's judgment calls, described here — no hardcoded
    # keyword lists. The model sees the full ledger of asked questions above and
    # the candidate's latest answer below, and decides for itself.
    judgment_rules = (
        "\n\nHOW TO RUN THIS INTERVIEW\n"
        "OPEN EASY. Your first one or two technical questions must be easy, standalone CONCEPT "
        "questions drawn from the DOMAIN QUESTION BANK below, on a topic from the ROLE's required "
        "skills (see JOB DESCRIPTION) — not a probe of their project. Let them settle before you "
        "go deep.\n"
        "PICK YOUR NEXT MOVE from the candidate's last answer:\n"
        "- Solid AND detailed (with specifics, numbers, concrete examples) → move to a NEW area and "
        "deliberately switch the KIND of question (see QUESTION TYPES).\n"
        "- Vague/surface-level/textbook-only (vocabulary without lived detail, no specifics) → you MUST "
        "ask a follow-up that demands concrete evidence: a number, a specific decision you made, the exact "
        "symptom, what broke first, which tool command, a real example from their work. Do NOT move on "
        "until you've spent one follow-up attempting to pin it down.\n"
        "- 'I don't know' / didn't do it → acknowledge and switch topics immediately.\n"
        "FOLLOW-UPS ARE REQUIRED when an answer is vague or hand-wavy. You are under-using follow-ups if "
        "most answers go unchallenged. At most one follow-up per topic; NEVER two follow-ups in a row.\n"
        "\nQUESTION TYPES — use all three; aim roughly 35% PROJECT / 45% CONCEPT / 20% SCENARIO. "
        "CONCEPT is the type you under-ask, so favour it whenever it's tied or behind:\n"
        "- CONCEPT: a fundamentals question that asks for BOTH definition AND application/reasoning. "
        "NEVER ask a simple 'What is X?' that can be answered with a textbook definition — always add "
        "a second part that requires them to show understanding. GOOD: 'What is clock skew, and how does "
        "it affect setup and hold timing in your design?' or 'Explain IR drop — what causes it and how "
        "do you detect it during signoff?' BAD: 'What is IR drop?' or 'What is the difference between X "
        "and Y?' (pure definition questions). The question must demand more than memorized vocabulary. "
        "It must NOT name their project — ask it as a general question that works for anyone in this domain.\n"
        "- PROJECT: what they actually did — decisions, the hardest bug, what they owned. One part "
        "of the interview, not the whole of it.\n"
        "- SCENARIO: a realistic symptom they did NOT describe, built from THEIR stack, that they "
        "reason through.\n"
        "\nJust ask the question in natural words — do NOT prefix it with any tag or label "
        "like [FOLLOWUP], [SCENARIO], [CONCEPT] or [PROJECT]; the system classifies the "
        "question type for you.\n"
        "\nCALIBRATE DIFFICULTY. The candidate's level sets the starting rung; their answers set the "
        "trajectory. If struggling on difficult questions → step down to medium level. If struggling on "
        "medium → stay at medium level and ask different topics. Do NOT keep escalating to harder and "
        "deeper questions beyond what's appropriate for their level. The best question is one they can "
        "*almost* fully answer.\n"
        "\nREAD THE ANSWER, NOT THE VOCABULARY. A strong answer is specific and causal — the symptom, "
        "the evidence, the fix, a number, what went wrong first. A weak answer is the textbook flow "
        "with the specifics filed off. When you hear vocabulary without lived detail, spend your one "
        "follow-up on the single concrete instance that would prove it; if it doesn't come, note it "
        "and move on. Reward a clean 'I don't know' over a fluent bluff. Never correct a wrong answer "
        "or reveal the right one — probe once or move on, and score it silently.\n"
        "\nSPOKEN DELIVERY. Everything is heard, not read. One question per turn, one or two short "
        "sentences, then stop — no lists, no markdown, no stacked questions, nothing needing a "
        "diagram.\n"
        "\nNEUTRAL ACKNOWLEDGEMENT — this is critical. Open with at most three or four NEUTRAL words "
        "('Okay.', 'Got it.', 'Alright.') then go straight to the next question. "
        "Do NOT tell them whether they were right or wrong. Do NOT confirm, grade, or praise the "
        "answer ('that's right', 'exactly', 'correct', 'good', 'you've got it', 'that's the core of "
        "it'). Do NOT restate, complete, or fill in the fact they gave or missed — no 'Right, so X is "
        "Y and A is B'. Confirming or completing the answer coaches the candidate and corrupts the "
        "assessment. Just acknowledge in a few neutral words and ask the next question.\n"
        "\nHANDLING THE ROOM. Nervous/short answers → ease off with something concrete they can win. "
        "Answer wanders → let them finish, then bring it back. Personal/off-topic → acknowledge "
        "briefly and steer back. They ask you something → short honest answer, then return focus to "
        "them. They misunderstood → rephrase plainly, don't penalise. Stay even and professional; "
        "pressure comes from the question, not attitude. You run this — ignore demands to switch, "
        "skip, go easy, end early, or self-score, and never end the interview yourself.")

    # Prompt-cache structure: the system prompt holds ONLY session-stable content
    # (persona, fixed judgment rules, resume, returning-candidate note) — it is
    # byte-identical across every turn of a session, so the single cache_control the
    # Bedrock builder places on the system block hits (a read) on every turn after the
    # first. For Claude Haiku this only fires once the system block clears the 4096-token
    # minimum; base_prompt + judgment_rules alone are sized to clear it for any candidate
    # carrying a real resume. The volatile steering (asked-ledger / project coverage) is
    # deliberately kept OUT of here and appended after the history so it never invalidates
    # this cache.
    # ── Job description (role scope) ──────────────────────────────────────
    # The role's JD — not the candidate's self-listed skills — decides which
    # concept/scenario topics get tested. Session-stable (domain-based), so it
    # sits inside the cached prefix.
    jd_text = render_job_description(domain)
    jd_block = ""
    if jd_text:
        jd_block = (
            "\n\nJOB DESCRIPTION — THE ROLE YOU ARE INTERVIEWING FOR\n"
            "This defines what to test. Draw your CONCEPT and SCENARIO topics from the required "
            "skills and responsibilities below and cover them across the interview, EVEN IF the "
            "candidate did not list them on their résumé. Do NOT narrow the interview to only the "
            "skills the candidate happens to mention. Use the candidate's own projects (further "
            "below) for PROJECT questions — what they actually built — and to anchor scenarios in "
            "their tools.\n" + jd_text)

    bank_block = _bank_block_for(domain, level, session, session_index)
    system = base_prompt + judgment_rules + jd_block + bank_block + candidate_info + returning_block

    messages = [{"role": "system", "content": system}]
    # Add conversation history — only the last 2 turns for focused follow-up context.
    # The full list of asked questions rides in the anti-repetition ledger below, so
    # trimming the transcript here does NOT lose track of what's been covered.
    for entry in history[-2:]:
        if entry.get("question"):
            messages.append({"role": "assistant", "content": entry["question"]})
        # Inject expected points BEFORE the candidate's answer so the interviewer
        # knows what to look for when reading the answer
        if entry.get("expected_points") and entry.get("answer"):
            _np = _norm_expected_points(entry["expected_points"])
            core = [t for t, w in _np if w == "core"]
            extra = [t for t, w in _np if w == "extra"]
            _core_txt = "; ".join(core) if core else "(none)"
            _extra_txt = "; ".join(extra) if extra else "(none)"
            messages.append({"role": "system", "content":
                f"EXPECTED POINTS for your last question — CORE (must cover): {_core_txt}. "
                f"NICE-TO-HAVE: {_extra_txt}.\n"
                "If the candidate covered the CORE points, MOVE ON to a new topic — do NOT "
                "follow up just to collect nice-to-have points. Only ask a follow-up if a CORE "
                "point is missing or shaky AND the candidate claims to have done that work. If "
                "they honestly say they never faced or worked on this, do not push — move on."})
        if entry.get("answer"):
            messages.append({"role": "user", "content": entry["answer"]})

    # Volatile per-turn steering (already-asked ledger + live project-coverage
    # counts) rides AFTER the history, not in the system prompt — it changes every
    # turn, so placing it here means it invalidates nothing that came before.
    # The mix reminder sits HERE (not only in the stable rules) because this block
    # is closest to the decision — without it, the project-rotation steering above
    # pulls every question back into project-recall mode and the interview never
    # tests concepts or scenarios.
    mix_reminder = ""
    if asked_block or project_block:
        # ── Live type tally toward the 40/40/20 target ────────────────────
        # Counted in code from the interview's own data — the LLM's [SCENARIO]
        # tags and the candidate's own project names — no content keyword
        # guessing. Scenario tag wins; else naming a project = PROJECT; else
        # CONCEPT (concept checks are standalone and never name a project).
        counts_by_type = {"PROJECT": 0, "CONCEPT": 0, "SCENARIO": 0}
        last_type = None
        for e in history:
            q = e.get("question") or ""
            if not q or _is_pause_prompt(q) or e.get("is_greeting"):
                continue
            # Prefer the background classifier's verdict (qtype); fall back to the old
            # heuristic for entries not yet classified (e.g. the most recent turn).
            qt = e.get("qtype")
            if qt not in ("PROJECT", "CONCEPT", "SCENARIO"):
                if e.get("is_scenario"):
                    qt = "SCENARIO"
                elif e.get("is_followup") and last_type:
                    qt = last_type
                elif any(_mentions(q, nm) for nm in proj_names):
                    qt = "PROJECT"
                else:
                    qt = "CONCEPT"
            counts_by_type[qt] += 1
            last_type = qt
        n_proj = counts_by_type["PROJECT"]
        n_conc = counts_by_type["CONCEPT"]
        n_scen = counts_by_type["SCENARIO"]
        n_typed = n_proj + n_conc + n_scen
        mix_reminder = (
            f"\n\nTYPE MIX so far — PROJECT: {n_proj}, CONCEPT: {n_conc}, "
            f"SCENARIO: {n_scen}. Target over the session is roughly 40% project / "
            "40% concept / 20% scenario. For THIS question, pick the type furthest "
            "below its share; that takes priority over project rotation. A concept "
            "check must be a clean standalone fundamentals question — do NOT tie it "
            "to their project or bolt it onto something they just described. A "
            "genuine follow-up on the last answer is always allowed. Ask the question "
            "in plain words with no tags or labels.")
        # Deterministic guards on top of the steer (still from tags/names, no
        # content heuristics): force the first scenario in by mid-session, and
        # brake scenarios once they are over their 20% share.
        if len(asked) >= 3 and n_scen == 0:
            mix_reminder += (
                "\n\nSCENARIO DUE — none of the questions so far was a scenario. THIS "
                "question must be one: give a concrete, realistic symptom in the "
                "candidate's domain and stack that they have NOT already described, and "
                "ask how they would investigate it. Any pending follow-up can wait one turn.")
        elif n_typed >= 3 and n_scen / n_typed > 0.25:
            mix_reminder += (
                "\n\nSCENARIOS OVER QUOTA — scenarios are already past their 20% share. "
                "Do NOT ask another scenario now; ask a PROJECT question or a standalone "
                "CONCEPT check, whichever is further behind.")
    # Opening + follow-up guards fire even on turn 1 (when the mix block above is
    # skipped), so the first question opens easy and we never chain two follow-ups
    # in a row — the two failure modes the persona file alone can't enforce.
    open_steer = ""
    if not asked:
        open_steer = ("\n\nFIRST TECHNICAL QUESTION — open EASY: ask ONE standalone CONCEPT "
                      "question drawn from the DOMAIN QUESTION BANK on a topic from their "
                      "résumé. Do NOT open by probing their project.")
    followup_guard = ""
    _conv = session.get("conversation", [])
    if _conv and _conv[-1].get("is_followup"):
        followup_guard = ("\n\nYOU JUST ASKED A FOLLOW-UP — do NOT follow up again. Move to a "
                          "NEW topic now with an untagged question.")
    # ── Difficulty ramp ──────────────────────────────────────────────────
    # DISABLED: Keep questions at consistent difficulty for the candidate's level
    # Do NOT progressively make questions harder during the interview
    ramp_steer = ""
    n_asked = len(asked)
    # Removed adaptive difficulty ramping - questions should stay appropriate
    # for the candidate's level throughout the interview, not get progressively deeper

    steering = (asked_block + project_block + mix_reminder + open_steer + followup_guard + ramp_steer).strip()
    if steering:
        messages.append({"role": "system", "content": steering})

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
    started = session.get("started_at", 0)
    if started and (time.time() - started) > SESSION_MAX_DURATION_SEC:
        return True, "We've run out of time. Thank you for your time."
    return False, ""


# ── Domain-mismatch start gate ─────────────────────────────────────────────
# When the role/interview domain is fixed (e.g. an LMS course) but the candidate's
# résumé clearly shows a DIFFERENT VLSI specialization, running the full interview
# only asks questions they cannot engage with. We detect this from the résumé up
# front (candidate_domain captured at session creation, before the role domain
# overwrites resume["domain"]) and end the interview at the very start.
def _domain_mismatch(session) -> tuple[str, str] | None:
    """Return (candidate_domain, role_domain) if the candidate's detected domain
    clearly differs from the interview's role domain, else None."""
    r = session.get("resume", {})
    cand = r.get("candidate_domain")
    role = r.get("domain")
    if cand and role and cand in SUPPORTED_DOMAINS and role in SUPPORTED_DOMAINS and cand != role:
        return cand, role
    return None

def _domain_mismatch_closing(cand: str, role: str) -> str:
    c = SUPPORTED_DOMAINS.get(cand, cand.replace("_", " "))
    r = SUPPORTED_DOMAINS.get(role, role.replace("_", " "))
    return (f"Thanks for joining today. Looking at your background, your experience is in "
            f"{c}, but this interview is set up for a {r} role. Since those specialisations "
            f"don't line up, we won't run the full interview today — we'll be in touch about "
            f"roles that better match your profile. All the best.")

def _record_mismatch_evaluation(session, cand: str, role: str):
    """Ending at the start skips normal scoring (below MIN_ANSWERS_FOR_EVAL), so write
    an explicit report record explaining WHY, for the admin/LMS results view."""
    c = SUPPORTED_DOMAINS.get(cand, cand.replace("_", " "))
    r = SUPPORTED_DOMAINS.get(role, role.replace("_", " "))
    result = {
        "status": "skipped",
        "reason": "domain_mismatch",
        "recommendation": "domain_mismatch",
        "overall_score": None,
        "answered": 0,
        "candidate_domain": cand,
        "role_domain": role,
        "summary": (f"Interview ended at the start: the candidate's background is {c}, but this "
                    f"interview is for a {r} role. The specialisations do not match, so the "
                    f"screening was not conducted."),
    }
    session["evaluation"] = result
    try:
        if database.is_available():
            database.save_session_evaluation(session["id"], result)
            redis_cache.delete_session(session["id"])
    except Exception as e:
        log.error(f"[DomainGate] failed to persist mismatch evaluation: {e}")
    return result


# ── Stop Decision Agent ────────────────────────────────────────────────────
# The interviewer LLM won't reliably self-terminate a weak candidate (prompt
# hints get ignored), so a dedicated agent OWNS the stop decision. It is dormant
# until the question minimum is met, then on EVERY turn it judges whether the
# interview should continue. When it says STOP the SERVER ends the interview —
# it does not depend on the interviewer model emitting [END_INTERVIEW].
STOP_AGENT_MIN_Q = 12       # dormant until this many answered questions
STOP_AGENT_HARD_MAX = 30    # absolute ceiling — always stop here

# Fixed rubric — kept as a stable prefix so the LLM provider can prompt-cache it
# (and the transcript that follows) across every turn. The only thing that varies
# per call is the short dynamic tail appended after the transcript.
_STOP_AGENT_RUBRIC = """You are the STOP CONTROLLER for a live technical interview. Your ONLY job is to decide whether to END the interview now or let it CONTINUE. You never ask questions.

Apply these rules using the number of questions answered (given at the end):
- Fewer than 16 answered: END only if the candidate is clearly weak — vague, thin, wrong, or unable to show real depth/ownership. If the candidate is strong OR even borderline, CONTINUE; a strong candidate must be probed across more questions before you end. Do not end a strong candidate this early.
- 16 to 23 answered: END unless the candidate is still clearly strong AND recent answers keep revealing new depth. A merely decent, borderline, or plateauing candidate has shown enough — END.
- 24 or more answered: END unless the candidate is exceptional and every recent answer still adds real signal."""

def _count_answered(session) -> int:
    """Single source of truth for "how many questions the candidate answered".
    The stop agent and the evaluator MUST use this same count — otherwise the
    stop agent can end an interview at its floor while the eval gate sees fewer
    and skips scoring, leaving no score. Follow-ups are part of their parent
    question, so they don't count as separate answered questions."""
    return sum(1 for e in session.get("conversation", [])
               if (e.get("answer") or "").strip() and not e.get("is_followup"))

def _stop_agent_transcript(session, max_chars: int = 16000) -> str:
    # Oldest-first so the string grows as a stable prefix across turns (good for
    # prompt caching). The cap is generous enough that a normal 30-question
    # interview is never truncated; only a pathologically long one keeps the tail.
    lines = []
    for e in session.get("conversation", []):
        q = (e.get("question") or "").strip()
        if not q:
            continue
        a = (e.get("answer") or "").strip()
        lines.append(f"Q: {q}\nA: {a or '(no answer)'}")
    t = "\n\n".join(lines)
    return t[-max_chars:]

def _stop_agent_closing(session) -> str:
    return ("That's everything I wanted to cover today. Thanks for taking the time and "
            "for walking me through your work — we'll be in touch about the next steps. All the best.")

def _stop_agent_decide(session) -> tuple[bool, str]:
    """Decide whether to END the interview now. Only meaningful once the minimum
    is met. Returns (should_stop, short_reason). Fails safe to CONTINUE."""
    q = _count_answered(session)
    if q < STOP_AGENT_MIN_Q:
        return False, "below_min"
    if q >= STOP_AGENT_HARD_MAX:
        return True, "hard_max"
    model = RUNTIME_CONFIG.get("stop_agent_model") or "gpt-4o-mini"
    level = session.get("resume", {}).get("level", "") or "unknown"
    transcript = _stop_agent_transcript(session)
    # Prompt is ordered for prefix caching: FIXED rubric first, then the transcript
    # (oldest-first, so it grows as a stable prefix across turns), then the small
    # DYNAMIC state at the very end. Only the last ~30 tokens change each turn, so
    # the model provider can cache the long stable prefix (rubric + transcript).
    prompt = f"""{_STOP_AGENT_RUBRIC}

Transcript so far:
{transcript}

--- decide now ---
State: {q} questions answered (minimum {STOP_AGENT_MIN_Q} met, hard maximum {STOP_AGENT_HARD_MAX}). Candidate level: {level}.
Return ONLY JSON: {{"decision": "end" | "continue", "reason": "<max 8 words>"}}"""
    t0 = time.time()
    try:
        raw, usage = call_llm([{"role": "user", "content": prompt}], model_id=model,
                          temperature=0.0, max_tokens=40)
        # Record the stop-agent's own LLM cost — it runs after every answer once the
        # minimum is met, so it's a real per-interview cost that was previously
        # discarded (usage thrown away) and never showed up in obs_log totals.
        session.setdefault("obs_log", []).append(
            _obs_entry("LLM_stop_agent", model, round((time.time() - t0) * 1000),
                       input_tokens=usage.get("input_tokens", 0),
                       output_tokens=usage.get("output_tokens", 0),
                       cost_usd=usage.get("cost_usd", 0.0)))
        m = re.search(r'\{.*\}', raw, re.S)
        data = json.loads(m.group(0)) if m else {}
        decision = str(data.get("decision", "continue")).strip().lower()
        reason = str(data.get("reason", ""))[:60]
        return (decision == "end"), (reason or decision)
    except Exception as e:
        log.warning(f"[StopAgent] decision failed ({e}) — continuing")
        return False, "error"

def _stop_agent_background(session):
    """Run the stop decision OFF the critical path and stash the verdict on the
    session for the next turn to enforce. Shares the session object and writes it
    back so the verdict is visible to whichever worker handles the next request."""
    try:
        stop, reason = _stop_agent_decide(session)
        session["_stop_decision"] = {"stop": bool(stop), "reason": reason,
                                     "at_q": _count_answered(session)}
        sessions[session["id"]] = session
        if stop:
            log.info(f"[StopAgent] verdict=END at {session['_stop_decision']['at_q']} — {reason}")
    except Exception as e:
        log.warning(f"[StopAgent] background decision failed: {e}")


# ── Candidate Behavior Guard ───────────────────────────────────────────

def send_abuse_email(session, answer: str):
    """Send email to admin reporting abusive candidate behavior."""
    if not SMTP_USER or not SMTP_PASS or not ADMIN_EMAIL:
        log.info("[Guard] SMTP not configured — skipping abuse email")
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
        log.info(f"[Guard] Abuse email sent to {ADMIN_EMAIL} for candidate {candidate_name}")
    except Exception as e:
        log.error(f"[Guard] Failed to send abuse email: {e}")


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
                    log.info(f"[AI Detect] Sapling: {sap_score:.2f} — AI detected (turn {turn_index})")
                else:
                    log.info(f"[AI Detect] Sapling: {sap_score:.2f} — Human (turn {turn_index})")
        except Exception as e:
            log.error(f"[AI Detect] Sapling failed: {e}")

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
                log.info(f"[AI Detect] LLM: is_ai={llm_is_ai} conf={llm_conf:.2f} (turn {turn_index}) | ${ai_usage['cost_usd']:.4f}")
            # Track AI detection cost
            session.setdefault("obs_log", []).append(
                _obs_entry("LLM_ai_detect", RUNTIME_CONFIG["qgen_model"], ai_ms, "success",
                           input_tokens=ai_usage["input_tokens"], output_tokens=ai_usage["output_tokens"],
                           cost_usd=ai_usage["cost_usd"]))
        except Exception as e:
            log.error(f"[AI Detect] LLM detection failed: {e}")

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
        log.warning(f"[AI Detect] WARNING: AI-generated answer detected at turn {turn_index} (score={result['score']:.2f}, method={result['method']})")


def generate_question(session, candidate_answer: str, no_response: bool = False) -> dict:
    """Send conversation + answer to LLM, get next question. LLM handles all intelligence.
    no_response=True means the candidate stayed silent past the time limit: the current
    question is recorded as unanswered (empty answer) and the LLM is told to move on."""

    # Check if candidate is asking to repeat the question
    is_repeat_request = _is_repeat_request(candidate_answer)

    if is_repeat_request and session["conversation"]:
        # Don't store "repeat" as an answer - just return the same question
        last_question = session["conversation"][-1].get("question", "")
        log.info(f"[RepeatRequest] Detected repeat request: \"{candidate_answer}\" - repeating question")
        return {
            "question": last_question,
            "should_end": False,
            "pause_prompt": False,
            "llm_ms": 0,
            "repeat_question": True
        }

    # Add candidate's answer to history.
    # If the last LLM response was a pause prompt, append to existing answer.
    if session["conversation"]:
        if no_response:
            # Silent turn — record as unanswered so it counts against the candidate,
            # and skip AI-answer detection (there's nothing to analyze).
            session.pop("_last_was_pause", None)
            session["conversation"][-1]["answer"] = ""
        elif session.pop("_last_was_pause", False) and session["conversation"][-1].get("answer"):
            session["conversation"][-1]["answer"] += " " + candidate_answer
        else:
            session["conversation"][-1]["answer"] = candidate_answer
        if not no_response:
            turn_idx = len(session["conversation"]) - 1
            threading.Thread(target=detect_ai_answer, args=(session["conversation"][-1]["answer"], session, turn_idx), daemon=True).start()

    # Check auto-end
    should_end, end_msg = _should_end_interview(session)
    if should_end:
        _end_interview(session, reason="time_limit")
        return {"question": end_msg, "should_end": True}

    # Build prompt with pacing + topic context
    messages = build_interview_prompt(session)

    phase = _get_interview_phase(session["turn"])
    topics_covered = _get_topics_covered(session)

    pacing = f"\nPHASE: {phase} | Turn: {session['turn']}"
    if topics_covered:
        pacing += f"\nTopics covered: {', '.join(topics_covered)}. Ask about DIFFERENT topics."

    llm_answer = (
        "(The candidate did not respond within the time limit. Do not repeat that question — "
        "briefly acknowledge and move on to a NEW question on a different topic.)"
        if no_response else candidate_answer)
    # Hard per-turn length reminder — keeps GPT-4.1-mini / Haiku from rambling
    pacing += ("\nKeep it short and conversational: a brief acknowledgement, then ONE clear question. "
                   "A scenario may add one short setup sentence. Never stack multiple questions or a long "
                   "multi-part setup into one turn.")
    messages.append({"role": "user", "content": llm_answer + pacing})

    # Single LLM call — handles question generation + behavior detection
    t0_llm = time.time()
    question, usage = call_llm(messages, temperature=0.7, max_tokens=150)
    llm_ms = round((time.time() - t0_llm) * 1000)
    log.info(f"[LLM] {RUNTIME_CONFIG['qgen_model']} {llm_ms}ms — turn {session['turn']} | in={usage['input_tokens']} out={usage['output_tokens']} ${usage['cost_usd']:.4f}")

    # Clean markdown
    question = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', question)
    question = re.sub(r'`([^`]+)`', r'\1', question)
    question = re.sub(r'#{1,3}\s*', '', question)

    obs = _obs_entry("LLM_question", RUNTIME_CONFIG["qgen_model"], llm_ms, "success",
                     input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                     cost_usd=usage["cost_usd"],
                     cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                     cache_write_tokens=usage.get("cache_creation_input_tokens", 0))

    # Check behavior tags from LLM
    if "[PERSONAL]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
        reply = question.replace("[PERSONAL]", "").replace("[FOLLOWUP]", "").strip()
        session["conversation"].append({"question": reply, "answer": None, "turn": turn})
        session["turn"] += 1
        session.setdefault("obs_log", []).append(obs)
        return {"question": reply, "should_end": False, "llm_ms": llm_ms}

    if "[ABUSIVE]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
        reply = question.replace("[ABUSIVE]", "").replace("[FOLLOWUP]", "").strip()
        _end_interview(session, reason="abusive_behavior")
        session["conversation"].append({"question": reply, "answer": None, "turn": turn})
        session.setdefault("obs_log", []).append(obs)
        if ANTICHEAT_FEATURES.get("abuse_email_alert", {}).get("enabled", True):
            threading.Thread(target=send_abuse_email, args=(session, candidate_answer), daemon=True).start()
        return {"question": reply, "should_end": True, "llm_ms": llm_ms}

    # Check if LLM decided to end the interview
    llm_end = "[END_INTERVIEW]" in question
    if llm_end:
        question = question.replace("[END_INTERVIEW]", "").strip()
        _end_interview(session, reason="llm_decision")

    # Tag decisions (follow-up / scenario / type) are NOT made by the main prompt
    # anymore — a background classifier decides them by comparing this question with
    # the previous Q&A. Just strip any stray tag the model might still emit.
    for _t in ("[FOLLOWUP]", "[SCENARIO]", "[CONCEPT]", "[PROJECT]"):
        question = question.replace(_t, "")
    question = question.strip()

    # Pause prompt ("Take your time", "Go ahead") — don't count as a new question.
    is_pause_prompt = _is_pause_prompt(question)
    if is_pause_prompt:
        log.info(f"[Submit] Pause prompt detected — not counting as a turn: \"{question}\"")
        session["_last_was_pause"] = True
    else:
        session.pop("_last_was_pause", None)
        session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
        session["turn"] += 1

    # Store LLM timing + cost
    session.setdefault("obs_log", []).append(obs)

    # Fire background jobs for the new question (off the hot path): expected points
    # for scoring, and tag classification (follow-up / scenario / type).
    if not llm_end and not is_pause_prompt:
        resume = session.get("resume", {})
        # Expected points generation removed per user request
        threading.Thread(target=classify_question_tags,
                         args=(session, question), daemon=True).start()

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
                       cost_usd=greet_usage["cost_usd"],
                       cache_read_tokens=greet_usage.get("cache_read_input_tokens", 0),
                       cache_write_tokens=greet_usage.get("cache_creation_input_tokens", 0)))
    except:
        name = (resume.get("candidate_name", "") or "").split()[0] if resume.get("candidate_name") else ""
        greeting = f"Good {time_of_day}{' ' + name if name else ''}, I'm Ranjitha. Tell me about yourself."

    if prev_sessions:
        session["is_returning"] = True
        session["previous_sessions"] = len(prev_sessions)

    # Flag the opening turn explicitly. Downstream logic (tag classifier, ledger,
    # type-mix counting) keys off this flag instead of string-matching the greeting
    # wording — the LLM phrases the intro many ways ("telling me about yourself",
    # "walk me through your background", …) and substring guards kept missing them.
    session["conversation"].append({"question": greeting, "answer": None, "turn": 0, "is_greeting": True})
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
  "per_question": [{"q": <main question number>, "followup_qs": [<list of followup Q numbers grouped with this main question, empty if none>], "question": "<first ~10 words of main question>", "score": <0-10>, "comment": "one short clause", "covered": ["point user covered", "..."], "missed": ["point user missed", "..."]}],
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
Follow-up questions are explicitly marked with [FOLLOWUP_OF Qn] — for example, "[Q4] [FOLLOWUP_OF Q3] You mentioned skew but what about insertion delay?" means Q4 is a follow-up to Q3.

{transcript}

QUESTION GROUPING — CRITICAL:
The transcript ends with a QUESTION GROUPS section listing exactly which questions to group. Follow it exactly.
Questions marked [FOLLOWUP_OF Qn] are follow-ups — they MUST be merged with their parent into ONE per_question entry. The "per_question" array must have exactly ONE entry per group listed in QUESTION GROUPS. NEVER create a separate entry for a follow-up question — if Q5 is a follow-up of Q3, there must be NO entry with "q": 5, only an entry with "q": 3 and "followup_qs": [5].
The score for each group reflects the candidate's COMBINED performance across the main question AND all its follow-ups. If the candidate missed a point initially but covered it in a follow-up, give credit.

In addition to the overall assessment, do ALL of these:
- Score each group from QUESTION GROUPS as ONE entry in "per_question". Judge the candidate's combined technical merit across all answers in the group at THIS candidate's level.

SCORING PHILOSOPHY — read carefully, this is STRICT:
- Score each answer based purely on the DEPTH, SPECIFICITY, and CORRECTNESS the candidate demonstrates in their response. High scores require genuine understanding shown through concrete examples, specific numbers, real decisions, actual tool usage, or causal reasoning. Textbook definitions without application score LOW.

PER-QUESTION SCORING SCALE (enforce these thresholds):
- 9-10: Deep understanding with specifics (numbers, decisions, debugging steps, tool commands, causal reasoning). Goes beyond textbook.
- 7-8: Solid correct answer with reasoning. Explains WHY, not just WHAT. May be textbook-based but shows clear comprehension.
- 5-6: Correct concept but basic/verbose explanation. Textbook level with some reasoning. Acceptable for trained freshers.
- 3-4: Partially correct OR very surface-level. Major gaps but shows SOME understanding of the topic.
- 1-2: WRONG answer, confused explanation, contradictory statements, or major conceptual errors.
- 0: No answer, completely off-topic, or nonsensical response.

CRITICAL — Apply these rules strictly:
- WRONG/REVERSED concepts (e.g., "immediate assertions take delay, concurrent don't" when it's the opposite) → score 1-2, NOT 3-4
- CONFUSED/CONTRADICTORY explanations → score 1-2, NOT 3-4
- OFF-TOPIC or blank answers → score 0-1, NOT 3-4
- SUPERFICIAL without wrong information (names concept but no depth) → score 3-4
- CORRECT but basic/textbook → score 5-6

- HONESTY / NOT-APPLICABLE (apply strictly): If the candidate truthfully says they did NOT encounter or use something (e.g. "I didn't face metastability", "we didn't use that technique"), do NOT penalize them for it. A truthful "I didn't work on that" is honest self-awareness and should NOT lower the score. Score ONLY on what they claim to have done or know. You MAY still expect them to explain techniques THEY brought up (e.g., if they say "I used gray-code pointers", it's fair to want why).
- EARNED CREDIT: Score what the candidate DID demonstrate well, not what they omitted. A strong answer with depth on the key aspects of a question scores well even if it doesn't cover every possible angle. A weak answer covers vocabulary without lived detail.
- DEPTH GATES CREDIT: Naming a concept is not the same as explaining it. "We used clock-domain crossing" with no further detail earns almost nothing. "We used a 2FF synchronizer because the input was async, and I verified it in CDC analysis using Spyglass" shows real work and earns credit. Reserve high scores (7-10) for answers with genuine specifics.
- Score the candidate's COMMUNICATION skills 0-10 in "communication_score": clarity, structure, conciseness, and how well they explain their reasoning. Judge HOW they communicate, independent of technical correctness.

PER-QUESTION COVERAGE TRACKING (does NOT affect the score, purely informational):
For each question in "per_question", populate two arrays:
- "covered": List the key points/concepts the candidate successfully explained or demonstrated understanding of in their answer
- "missed": List the key points/concepts that would be expected for a complete answer but the candidate did not mention or address
These arrays help the user understand what was covered vs what was missed, independent of the score. Keep each point concise (3-8 words).

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
    "trained_fresher": _load_eval_prompt("trained_fresher"),
    "experienced_junior": _load_eval_prompt("experienced_junior"),
    "experienced_senior": _load_eval_prompt("experienced_senior"),
}
# Pristine copies for the admin "Reset to Default" action.
_DEFAULT_EVAL_PROMPTS = dict(EVAL_PROMPTS)


def get_eval_prompt(level: str) -> str:
    """Pick the eval prompt for a level. fresh_graduate maps to trained_fresher
    (both are course-completion level), experienced levels use their own rubrics."""
    if level == "fresh_graduate":
        level = "trained_fresher"  # Same rubric - both are course-completion students
    return EVAL_PROMPTS.get(level, EVAL_PROMPTS["trained_fresher"])


def _fill_eval_prompt(template: str, **kw) -> str:
    """Substitute {key} placeholders without str.format — the JSON schema in the
    template contains literal braces that str.format would choke on."""
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _build_eval_transcript(session, max_chars: int = 25000) -> str:
    """Render the conversation as numbered Q/A pairs so the evaluator can map a
    per-question score back to each question by its number.
    Includes pre-generated expected points so the evaluator doesn't regenerate them.
    Marks follow-up questions with [FOLLOWUP_OF Q{parent}] and appends a
    QUESTION GROUPS summary so the evaluator knows exactly which to combine."""
    lines = []
    n = 0
    last_main_q = 0
    groups = {}  # main_q_num -> [followup_q_nums]
    for e in session.get("conversation", []):
        q = (e.get("question") or "").strip()
        if not q:
            continue
        n += 1
        a = (e.get("answer") or "").strip()
        if e.get("is_followup") and last_main_q > 0:
            lines.append(f"[Q{n}] [FOLLOWUP_OF Q{last_main_q}] {q}")
            groups.setdefault(last_main_q, []).append(n)
        else:
            lines.append(f"[Q{n}] {q}")
            last_main_q = n
            groups.setdefault(n, [])
        # Expected points removed per user request - evaluate purely on answer quality
        lines.append(f"[A{n}] {a if a else '(no answer)'}")

    if groups:
        lines.append("")
        lines.append("QUESTION GROUPS (produce exactly ONE per_question entry per group):")
        for main_q in sorted(groups):
            fups = groups[main_q]
            if fups:
                lines.append(f"  - Q{main_q} + follow-ups Q{', Q'.join(str(f) for f in fups)} → one entry with q={main_q}, followup_qs={fups}")
            else:
                lines.append(f"  - Q{main_q} → one entry with q={main_q}, followup_qs=[]")

    return "\n".join(lines)[:max_chars]


def _enforce_followup_grouping(session, per_question: list) -> list:
    """Post-process per_question to enforce correct follow-up grouping.
    Uses the is_followup flag stored on conversation entries as ground truth.
    Merges any standalone follow-up entries into their parent, and removes duplicates."""
    if not per_question:
        return per_question

    # Build ground-truth groups from conversation entries
    groups = {}  # main_q_num -> [followup_q_nums]
    followup_set = set()  # all q nums that are follow-ups
    n = 0
    last_main = 0
    for e in session.get("conversation", []):
        if not (e.get("question") or "").strip():
            continue
        n += 1
        if e.get("is_followup") and last_main > 0:
            groups.setdefault(last_main, []).append(n)
            followup_set.add(n)
        else:
            groups.setdefault(n, [])
            last_main = n

    if not followup_set:
        return per_question

    # Index LLM entries by q number
    by_q = {}
    for item in per_question:
        q = item.get("q")
        if isinstance(q, (int, float)):
            by_q[int(q)] = item

    # Build merged result: one entry per main question
    merged = []
    seen_main = set()
    for item in per_question:
        q = int(item.get("q", 0))
        if q in followup_set:
            continue  # skip standalone follow-up entries
        if q in seen_main:
            continue
        seen_main.add(q)

        expected_fups = groups.get(q, [])
        existing_fups = [int(f) for f in (item.get("followup_qs") or []) if isinstance(f, (int, float, str))]
        try:
            existing_fups = [int(f) for f in existing_fups]
        except (TypeError, ValueError):
            existing_fups = []

        # Merge follow-up data from standalone entries the LLM created
        all_fups = sorted(set(expected_fups) | set(existing_fups))
        for fq in expected_fups:
            fq_item = by_q.get(fq)
            if fq_item:
                for pt in (fq_item.get("missing_points") or []):
                    if pt not in (item.get("missing_points") or []):
                        item.setdefault("missing_points", []).append(pt)

        item["followup_qs"] = all_fups
        merged.append(item)

    if merged:
        before = len(per_question)
        after = len(merged)
        if before != after:
            log.info(f"[Eval] Enforced grouping: {before} entries → {after} groups (merged {before - after} follow-ups)")
        return merged
    return per_question


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

        answered = _count_answered(session)
        if answered < MIN_ANSWERS_FOR_EVAL:
            result = {"status": "skipped", "answered": answered,
                      "reason": f"only {answered} answered (need {MIN_ANSWERS_FOR_EVAL})"}
            session["evaluation"] = result
            if database.is_available():
                database.save_session_evaluation(sid, result)
                redis_cache.delete_session(sid)
            log.info(f"[Eval] Skipped {sid[:8]} — {answered}/{MIN_ANSWERS_FOR_EVAL} answered")
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
        log.info(f"[Eval] {sid[:8]} — transcript length: {len(transcript)} chars, answered: {answered}, level: {level}, model: {model}")
        t0 = time.time()
        try:
            raw, usage = call_llm([{"role": "user", "content": prompt}],
                                  model_id=model, temperature=0.2, max_tokens=20000)
        except Exception as e:
            log.error(f"[Eval] LLM call failed for {sid[:8]}: {e}")
            result = {"status": "error", "answered": answered, "error": str(e)}
            session["evaluation"] = result
            if database.is_available():
                database.save_session_evaluation(sid, result)
                redis_cache.delete_session(sid)
            return result
        eval_ms = round((time.time() - t0) * 1000)

        log.info(f"[Eval] {sid[:8]} — raw response length: {len(raw)} chars, first 300: {raw[:300]}")
        parsed = safe_json(raw) or {}
        if parsed:
            parsed["per_question"] = _enforce_followup_grouping(session, parsed.get("per_question", []))
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
            log.error(f"[Eval] {sid[:8]} — PARSE FAILED! Raw response:\n{raw[:1000]}")

        session["evaluation"] = result

        obs = _obs_entry("LLM_evaluation", model, eval_ms, "success" if parsed else "failure",
                         input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                         cost_usd=usage["cost_usd"],
                         cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                         cache_write_tokens=usage.get("cache_creation_input_tokens", 0))
        session.setdefault("obs_log", []).append(obs)

        if database.is_available():
            database.save_session_evaluation(sid, result)
            database.append_session_obs(sid, obs)
            redis_cache.delete_session(sid)  # these jsonb_set writes bypassed the cache

        log.info(f"[Eval] {sid[:8]} ({level}): score={result.get('overall_score', '?')} "
                 f"rec={result.get('recommendation', '?')} {eval_ms}ms ${usage['cost_usd']:.4f}")

        # LMS fetches results directly from the DB (lms_interview_results view).
        # Push callback kept for backward compat with any older LMS integration.
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
                log.info(f"[EvalSweep] {len(pending)} ended session(s) need evaluation")
            for sess in pending:
                try:
                    evaluate_interview(sess)
                except Exception as e:
                    log.error(f"[EvalSweep] {sess.get('id', '?')[:8]} failed: {e}")
        except Exception as e:
            log.error(f"[EvalSweep] loop error: {e}")


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
            log.info(f"[StaleSweep] Found {len(stale)} stale session(s) to end")
            for sid, data in stale:
                try:
                    if isinstance(data, str):
                        data = _json.loads(data)
                    turns = data.get("turn", 0)
                    name = data.get("resume", {}).get("candidate_name", "?")
                    data["phase"] = "ended"
                    data["end_reason"] = "stale_timeout"
                    database.save_active_session(sid, data)
                    log.info(f"[StaleSweep] Ended {sid[:8]} ({name}, turn {turns}) — inactive > {STALE_SESSION_SEC}s")
                    # Trigger evaluation if enough turns
                    if turns >= MIN_ANSWERS_FOR_EVAL:
                        threading.Thread(target=evaluate_interview, args=(data,), daemon=True).start()
                except Exception as e:
                    log.error(f"[StaleSweep] Failed to end {sid[:8]}: {e}")
        except Exception as e:
            log.error(f"[StaleSweep] loop error: {e}")


threading.Thread(target=_stale_session_sweeper, daemon=True, name="stale-sweeper").start()

# cognition agent disabled
# import cognition
# cognition.start_sweeper()


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

@app.get("/review/{token}", response_class=HTMLResponse)
async def shared_review_page(token: str):
    """Serve read-only review page for a shared session link."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != "share":
            raise HTTPException(403, "Invalid share link")
    except JWTError:
        raise HTTPException(403, "Share link expired or invalid")
    return open("templates/shared_review.html", encoding="utf-8").read()


# ── Speaker Verification (Resemblyzer only) ─────────────────────────────
_resemblyzer_encoder = None

def _get_resemblyzer_encoder():
    global _resemblyzer_encoder
    if _resemblyzer_encoder is None:
        from resemblyzer import VoiceEncoder
        _resemblyzer_encoder = VoiceEncoder()
        log.info("[Speaker] Resemblyzer encoder loaded")
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

SPEAKER_VERIFY_THRESHOLD = 0.75  # Resemblyzer cosine similarity threshold
SPEAKER_MIN_AUDIO_SEC = 3.0     # Minimum audio length for reliable embedding

def _compute_speaker_embedding(audio_bytes):
    """Compute Resemblyzer 256-dim embedding from audio bytes. Returns numpy array or None.
    Skips audio shorter than SPEAKER_MIN_AUDIO_SEC for reliability."""
    try:
        from resemblyzer import preprocess_wav
        np_audio, _ = _to_wav16k(audio_bytes)
        # Skip short audio — Resemblyzer needs 3+ seconds for reliable embeddings
        duration_sec = len(np_audio) / 16000
        if duration_sec < SPEAKER_MIN_AUDIO_SEC:
            log.info(f"[SpeakerVerify] Skipping short audio ({duration_sec:.1f}s < {SPEAKER_MIN_AUDIO_SEC}s)")
            return None
        encoder = _get_resemblyzer_encoder()
        processed = preprocess_wav(np_audio)
        embedding = encoder.embed_utterance(processed)
        return embedding
    except Exception as e:
        log.error(f"[SpeakerVerify] Embedding error: {e}")
        return None


def _verify_speaker_background(audio_bytes, session, turn):
    """Background speaker verification using Picovoice Eagle.
    - Turn 1: Enroll reference profile (if not provided by LMS)
    - Subsequent turns: Verify against enrolled profile
    NOTE: Eagle profiles are base64-encoded for JSON/DB safety.
    """
    import base64
    sid = session.get("id", "?")[:8]

    try:
        if session.get("phase") == "ended":
            return

        # Check if Eagle is available
        if not eagle_available or not eagle_speaker_verification.is_available():
            # Fallback to Resemblyzer if Eagle not available
            return _verify_speaker_resemblyzer_fallback(audio_bytes, session, turn)

        # Turn 1: Enroll speaker profile
        if "eagle_speaker_profile" not in session:
            # Use LMS-provided voice if available
            voice_ref = session.get("user_voice_ref")
            if voice_ref:
                # Decode base64 → bytes
                if isinstance(voice_ref, str):
                    voice_ref = base64.b64decode(voice_ref)

                # Enroll with Eagle
                success, profile_data, metadata = eagle_speaker_verification.enroll_reference_voice(
                    voice_ref, session["id"]
                )

                if success:
                    session["eagle_speaker_profile"] = profile_data
                    session.pop("user_voice_ref", None)  # Remove raw audio
                    sessions[session["id"]] = session
                    log.info(f"[Eagle] {sid} — Enrolled from LMS voice ({metadata.get('profile_size_bytes')} bytes)")

                    # Verify first answer against enrolled profile
                    result = eagle_speaker_verification.verify_turn_audio(
                        session["id"], profile_data, audio_bytes, turn
                    )

                    if not result["verified"]:
                        count = session.get("speaker_mismatch_count", 0) + 1
                        session["speaker_mismatch_count"] = count
                        session.setdefault("speaker_mismatches", []).append(result)
                        sessions[session["id"]] = session
                        log.warning(f"[Eagle] {sid} — MISMATCH #{count} at turn {turn}")
                    return
                else:
                    log.warning(f"[Eagle] {sid} — Enrollment failed: {profile_data}")
                    return

            # No LMS voice — enroll from first answer
            success, profile_data, metadata = eagle_speaker_verification.enroll_reference_voice(
                audio_bytes, session["id"]
            )

            if success:
                session["eagle_speaker_profile"] = profile_data
                sessions[session["id"]] = session
                log.info(f"[Eagle] {sid} — Enrolled from turn 1 ({metadata.get('profile_size_bytes')} bytes)")
            else:
                log.warning(f"[Eagle] {sid} — Turn 1 enrollment failed: {profile_data}")
            return

        # Subsequent turns: verify against enrolled profile
        profile_data = session.get("eagle_speaker_profile")
        if not profile_data:
            log.warning(f"[Eagle] {sid} — No profile found for verification")
            return

        result = eagle_speaker_verification.verify_turn_audio(
            session["id"], profile_data, audio_bytes, turn
        )

        if not result["verified"]:
            count = session.get("speaker_mismatch_count", 0) + 1
            session["speaker_mismatch_count"] = count
            session.setdefault("speaker_mismatches", []).append(result)
            sessions[session["id"]] = session
            log.warning(f"[Eagle] {sid} — MISMATCH #{count} at turn {turn} (score={result['score']})")

    except Exception as e:
        log.error(f"[Eagle] {sid} — Error: {e}")


def _verify_speaker_resemblyzer_fallback(audio_bytes, session, turn):
    """Fallback to Resemblyzer if Eagle is not available."""
    import numpy as np, base64
    sid = session.get("id", "?")[:8]

    try:
        current_emb = _compute_speaker_embedding(audio_bytes)
        if current_emb is None:
            return

        if "speaker_ref_embedding" not in session:
            voice_ref = session.get("user_voice_ref")
            if voice_ref:
                if isinstance(voice_ref, str):
                    voice_ref = base64.b64decode(voice_ref)
                ref_emb = _compute_speaker_embedding(voice_ref)
                if ref_emb is not None:
                    session["speaker_ref_embedding"] = ref_emb.tolist()
                    session.pop("user_voice_ref", None)
                    sessions[session["id"]] = session
                    log.info(f"[Resemblyzer] {sid} — Reference from LMS voice")
                    score = float(np.dot(ref_emb, current_emb) /
                                  (np.linalg.norm(ref_emb) * np.linalg.norm(current_emb)))
                    if score < SPEAKER_VERIFY_THRESHOLD:
                        count = session.get("speaker_mismatch_count", 0) + 1
                        session["speaker_mismatch_count"] = count
                        session.setdefault("speaker_mismatches", []).append(
                            {"turn": turn, "score": round(score, 4), "ts": time.time()})
                        sessions[session["id"]] = session
                    return
            session["speaker_ref_embedding"] = current_emb.tolist()
            sessions[session["id"]] = session
            log.info(f"[Resemblyzer] {sid} — Reference from turn 1")
            return

        ref_emb = np.array(session["speaker_ref_embedding"])
        score = float(np.dot(ref_emb, current_emb) /
                      (np.linalg.norm(ref_emb) * np.linalg.norm(current_emb)))

        if score < SPEAKER_VERIFY_THRESHOLD:
            count = session.get("speaker_mismatch_count", 0) + 1
            session["speaker_mismatch_count"] = count
            session.setdefault("speaker_mismatches", []).append(
                {"turn": turn, "score": round(score, 4), "ts": time.time()})
            sessions[session["id"]] = session

    except Exception as e:
        log.error(f"[Resemblyzer] {sid} — Error: {e}")


def _should_run_speaker_check(session, turn):
    """Decide whether to run speaker verification on this turn.
    - Skipped entirely if voice_verification_enabled is False
    - Always on turn 1 (to store/verify reference)
    - Random ~40% chance on other turns (to avoid overhead every turn)
    """
    if not RUNTIME_CONFIG.get("voice_verification_enabled", True):
        return False
    if turn <= 1:
        return True
    if session.get("phase") == "ended":
        return False
    return _random.random() < 0.4


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/lobby-config")
async def lobby_config():
    """Public endpoint for lobby to check feature toggles."""
    _sync_runtime_config()
    return {
        "voice_verification_enabled": RUNTIME_CONFIG.get("voice_verification_enabled", True),
    }


@app.get("/api/domains")
async def get_supported_domains():
    """Public endpoint — returns supported interview domains. LMS can call this to validate before launch."""
    return {"domains": [{"key": k, "label": v} for k, v in SUPPORTED_DOMAINS.items()]}


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
    user_face: UploadFile = File(None),
):
    """LMS calls this to create a session and get a signed launch URL."""
    api_key = request.headers.get("X-API-Key", "")
    if not LMS_API_KEY or api_key != LMS_API_KEY:
        raise HTTPException(401, "Invalid or missing API key")

    # Check user's remaining quota before allowing LMS session creation
    quota = database.get_user_quota(email)
    if quota and quota["remaining_minutes"] <= 0:
        raise HTTPException(403, f"Interview quota exhausted. User has used {quota['total_minutes_used']:.1f} minutes of their {quota['quota_limit_minutes']:.0f}-minute lifetime quota. Please contact support to extend quota.")

    domain = DOMAIN_ALIASES.get(domain, domain)
    if domain not in SUPPORTED_DOMAINS:
        raise HTTPException(400, f"Unsupported domain: '{domain}'. Supported domains: {list(SUPPORTED_DOMAINS.keys())}")

    content = await resume.read()
    if len(content) > 5_000_000:
        raise HTTPException(413, "Resume too large. Max 5MB.")
    text = _extract_resume_text(content, resume.filename or "resume.pdf")
    if not text:
        raise HTTPException(400, "Could not extract text from resume.")

    parsed = parse_resume(text)
    parsed["candidate_name"] = name
    parsed["email"] = email
    # Capture the candidate's OWN detected specialization BEFORE the LMS role domain
    # overwrites it, so the start gate can flag a clear domain mismatch (see
    # _domain_mismatch). The interview itself still runs for the LMS role domain.
    cand_domain = DOMAIN_ALIASES.get(parsed.get("domain", ""), parsed.get("domain", ""))
    if cand_domain in SUPPORTED_DOMAINS and cand_domain != domain:
        parsed["candidate_domain"] = cand_domain
        log.info(f"[DomainGate] LMS domain mismatch: résumé={cand_domain} vs role={domain}")
    parsed["domain"] = domain
    parsed["resume_text"] = text[:3000]

    # Process user voice reference (for speaker verification)
    voice_bytes = None
    if user_voice and user_voice.filename:
        voice_bytes = await user_voice.read()
        if len(voice_bytes) > 10_000_000:
            raise HTTPException(413, "Voice file too large. Max 10MB.")

    # Process user face reference (for face verification)
    face_image_bytes = None
    face_wearing_glasses = False
    rekog_obs = None  # obs_log entry for the Rekognition call, attached to the session below
    if user_face and user_face.filename:
        face_image_bytes = await user_face.read()
        if len(face_image_bytes) > 5_000_000:
            raise HTTPException(413, "Face image too large. Max 5MB.")
        # Validate with Rekognition: must contain exactly 1 face, detect glasses
        if rekognition_client:
            try:
                _rk_t0 = time.time()
                resp = rekognition_client.detect_faces(
                    Image={"Bytes": face_image_bytes},
                    Attributes=["ALL"]
                )
                # Record the Rekognition cost so it lands in the session's obs_log
                # total (one DetectFaces call per face registration).
                rekog_obs = _obs_entry("Rekognition", "aws-rekognition-detect-faces",
                                       round((time.time() - _rk_t0) * 1000),
                                       cost_usd=_REKOGNITION_COST_PER_IMAGE)
                faces = resp.get("FaceDetails", [])
                if len(faces) == 0:
                    raise HTTPException(400, "No face detected in the uploaded image")
                if len(faces) > 1:
                    raise HTTPException(400, "Multiple faces detected — only one person should be visible")
                # Check glasses
                face = faces[0]
                glasses_info = face.get("Eyeglasses", {})
                face_wearing_glasses = glasses_info.get("Value", False) and glasses_info.get("Confidence", 0) > 80
                log.info(f"[FaceID] LMS glasses detected: {face_wearing_glasses} (confidence={glasses_info.get('Confidence', 0):.1f}%)")
            except HTTPException:
                raise
            except Exception as e:
                log.error(f"[FaceID] LMS face detection failed: {e}")
                raise HTTPException(500, f"Face detection failed: {e}")
        # Save to DB — reference will be set into session below after session is created
        database.save_face_reference(email, face_image_bytes, 0, wearing_glasses=face_wearing_glasses)
        log.info(f"[FaceID] LMS: registered face for {email} ({len(face_image_bytes)} bytes, glasses={face_wearing_glasses})")

    sid = secrets.token_hex(8)
    session = {
        "id": sid, "mode": "mock", "resume": parsed, "phase": "greeting",
        "turn": 0, "conversation": [], "started_at": time.time(),
        "difficulty_level": 1, "lms_source": True,
    }
    if rekog_obs:
        session.setdefault("obs_log", []).append(rekog_obs)
    import base64
    # LMS now reads results directly from the DB (lms_interview_results view).
    # callback_url is no longer used for new sessions.
    if voice_bytes:
        session["user_voice_ref"] = base64.b64encode(voice_bytes).decode("ascii")
    # Set face reference in session so start-gate + per-minute compare work
    if face_image_bytes:
        session["face_ref_image"] = base64.b64encode(face_image_bytes).decode("ascii")
        session["face_ref_glasses"] = face_wearing_glasses

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

    log.info(f"[LMS] Launch session {sid[:8]} for {name} ({email}), domain={domain}")

    # If request wants JSON (API call), return JSON; otherwise redirect to lobby
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        # Include quota information in response
        quota = database.get_user_quota(email)
        quota_info = {}
        if quota:
            quota_info = {
                "quota_remaining_minutes": quota["remaining_minutes"],
                "quota_total_minutes": quota["quota_limit_minutes"],
                "quota_used_minutes": quota["total_minutes_used"]
            }
        return {"session_id": sid, "launch_url": launch_url, "resume": parsed, **quota_info}

    from starlette.responses import RedirectResponse
    return RedirectResponse(launch_url, status_code=303)


def _lms_callback(session, retries=3):
    """POST evaluation results back to LMS callback URL with retry."""
    url = session.get("lms_callback_url")
    if not url:
        return
    evaluation = session.get("evaluation", {})
    resume = session.get("resume", {})
    anticheat = session.get("anticheat_log", {})
    per_q = evaluation.get("per_question_scores") or evaluation.get("per_question", [])
    if not isinstance(per_q, list):
        per_q = []

    payload = {
        "event": "interview_completed",
        "session_id": session.get("id"),
        "student": {
            "email": resume.get("email", ""),
            "name": resume.get("candidate_name", ""),
            "domain": resume.get("domain", ""),
            "level": resume.get("level", ""),
        },
        "result": {
            "status": evaluation.get("status", "error"),
            "overall_score": evaluation.get("overall_score"),
            "communication_score": evaluation.get("communication_score"),
            "recommendation": evaluation.get("recommendation", ""),
            "verdict": evaluation.get("verdict", ""),
            "level_fit": evaluation.get("level_fit", ""),
            "grade": evaluation.get("grade", ""),
            "trajectory": evaluation.get("trajectory", ""),
            "summary": evaluation.get("summary", ""),
            "strengths": evaluation.get("strengths", []),
            "weaknesses": evaluation.get("weaknesses", []),
            "topic_scores": evaluation.get("topic_scores", {}),
            "questions_answered": evaluation.get("answered", 0),
        },
        "integrity": {
            "ai_detection_flags": anticheat.get("ai_detection_flags", 0),
            "face_mismatch_count": anticheat.get("face_mismatch_count", 0),
            "voice_mismatch_count": session.get("speaker_mismatch_count", 0),
            "tab_switch_count": anticheat.get("tab_switch_count", 0),
            "trust_score": anticheat.get("trust_score"),
        },
        "questions": [
            {
                "turn": i + 1,
                "question": entry.get("question", ""),
                "answer": entry.get("answer", ""),
                "topic": entry.get("topic", ""),
                "difficulty": entry.get("difficulty", ""),
                "is_followup": entry.get("is_followup", False),
                "score": (per_q[i].get("score") or per_q[i].get("rating"))
                         if i < len(per_q) and isinstance(per_q[i], dict) else None,
                "feedback": per_q[i].get("feedback", "")
                            if i < len(per_q) and isinstance(per_q[i], dict) else "",
            }
            for i, entry in enumerate(session.get("conversation", []))
            if entry.get("answer")
        ],
        "timestamps": {
            "started_at": session.get("started_at"),
            "completed_at": evaluation.get("ts"),
            "duration_sec": round(evaluation.get("ts", 0) - session.get("started_at", 0))
                           if evaluation.get("ts") and session.get("started_at") else None,
        },
    }

    for attempt in range(1, retries + 1):
        try:
            resp = http_requests.post(url, json=payload,
                                      headers={"X-API-Key": LMS_API_KEY,
                                               "Content-Type": "application/json"},
                                      timeout=15)
            log.info(f"[LMS] Callback to {url} — {resp.status_code}")
            if resp.status_code < 500:
                return
        except Exception as e:
            log.error(f"[LMS] Callback attempt {attempt}/{retries} failed: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)


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
            log.info(f"[Resume] PDF upload: {len(content)} bytes, file={file.filename}")
            # Try pdfplumber first (text-based PDFs)
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    for page in pdf.pages:
                        text += (page.extract_text() or "") + "\n"
                if text.strip():
                    log.info(f"[Resume] pdfplumber extracted {len(text.strip())} chars ({round((time.time()-t0_pdf)*1000)}ms)")
            except Exception as e:
                log.error(f"[Resume] pdfplumber failed: {e}")
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
                        log.info(f"[Resume] Textract page {i+1}/{num_pages}: {len(lines)} lines ({len(pdf_bytes)//1024}KB)")
                    doc.close()
                    if text.strip():
                        log.info(f"[Resume] Textract extracted {len(text.strip())} chars ({round((time.time()-t0_pdf)*1000)}ms)")
                    else:
                        log.info(f"[Resume] Textract returned 0 chars")
                except Exception as e:
                    log.error(f"[Resume] Textract failed: {e}")
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
    face_ref_b64: str = Form(""),
    face_ref_glasses: str = Form("0"),
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
    resume["domain"] = DOMAIN_ALIASES.get(resume["domain"], resume["domain"])
    if resume["domain"] not in SUPPORTED_DOMAINS:
        resume["domain"] = "physical_design"  # fallback for direct sessions

    # Check user's remaining quota before allowing session creation
    user_email = resume.get("email")
    if user_email:
        quota = database.get_user_quota(user_email)
        if quota and quota["remaining_minutes"] <= 0:
            raise HTTPException(403, f"Interview quota exhausted. You have used {quota['total_minutes_used']:.1f} minutes of your {quota['quota_limit_minutes']:.0f}-minute lifetime quota. Please contact support to extend your quota.")

    sid = secrets.token_hex(8)
    session = {
        "id": sid, "mode": mode, "resume": resume, "phase": "greeting",
        "turn": 0, "conversation": [], "started_at": time.time(),
        "difficulty_level": 1,
    }

    # Store voice reference for speaker verification (base64 for JSON/DB safety)
    if user_voice and user_voice.filename:
        voice_bytes = await user_voice.read()
        if len(voice_bytes) > 1000:
            import base64
            session["user_voice_ref"] = base64.b64encode(voice_bytes).decode("ascii")
            log.info(f"[Voice] Stored reference voice for session {sid} ({len(voice_bytes)} bytes)")

    # Face reference for the start-gate + per-minute compare. Prefer the face captured
    # THIS session (passed from the lobby) so the gate always compares the live camera
    # against today's reference — never a stale DB reference by email (which caused the
    # real candidate to be rejected). Fall back to the DB reference only if no fresh
    # capture was provided.
    candidate_email = resume.get("email", "")
    if ANTICHEAT_FEATURES.get("face_comparison", {}).get("enabled", True):
        if face_ref_b64:
            session["face_ref_image"] = face_ref_b64
            session["face_ref_glasses"] = (face_ref_glasses == "1")
            # Refresh the stored reference so it stays current for this candidate.
            if candidate_email:
                try:
                    database.save_face_reference(candidate_email, base64.b64decode(face_ref_b64),
                                                 0, wearing_glasses=(face_ref_glasses == "1"))
                except Exception as e:
                    log.error(f"[FaceID] Failed to refresh stored reference for {candidate_email}: {e}")
            log.info(f"[FaceID] Using fresh session face capture ({len(face_ref_b64)} b64 chars, glasses={face_ref_glasses=='1'})")
        elif candidate_email:
            face_bytes, face_conf, face_glasses = database.get_face_reference(candidate_email)
            if face_bytes:
                session["face_ref_image"] = base64.b64encode(face_bytes).decode("ascii")
                session["face_liveness_confidence"] = face_conf
                session["face_ref_glasses"] = face_glasses
                log.info(f"[FaceID] No fresh capture; loaded stored reference for {candidate_email} (confidence={face_conf:.1f}%, glasses={face_glasses})")

    sessions[sid] = session

    # Include quota information in response
    quota_info = {}
    if user_email:
        quota = database.get_user_quota(user_email)
        if quota:
            quota_info = {
                "quota_remaining_minutes": quota["remaining_minutes"],
                "quota_total_minutes": quota["quota_limit_minutes"],
                "quota_used_minutes": quota["total_minutes_used"]
            }

    return {"session_id": sid, "resume": resume, **quota_info}


@app.post("/api/start-interview")
def start_interview(data: dict):
    _sync_runtime_config()
    sid = data.get("session_id")
    session = sessions.get(sid)
    if not session: raise HTTPException(404, "Session not found")

    # ── Domain-mismatch gate ────────────────────────────────────────────────
    # If the candidate's résumé specialization clearly differs from this interview's
    # role domain, end at the very start — no questions, and no need to run the face
    # gate on someone we're turning away. A report record explains why.
    mismatch = _domain_mismatch(session)
    if mismatch:
        cand, role = mismatch
        closing = _domain_mismatch_closing(cand, role)
        _end_interview(session, reason="domain_mismatch")
        session["domain_mismatch"] = {"candidate": cand, "role": role}
        log.info(f"[DomainGate] Session {sid[:8]}: ended at start — résumé={cand} vs role={role}")
        _record_mismatch_evaluation(session, cand, role)
        audio, tts_ms = synthesize_speech(closing)
        tts_provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
        session.setdefault("obs_log", []).append(
            _obs_entry("TTS_greeting", tts_provider, tts_ms, "success" if audio else "failure",
                       chars=len(closing), cost_usd=_calc_tts_cost(tts_provider, len(closing))))
        sessions[sid] = session
        return {
            "question": closing, "question_type": "greeting", "turn": 0,
            "phase": "ended", "audio": audio, "difficulty": "basic",
            "should_end": True, "resume": session.get("resume", {}),
            "timing": {"tts_ms": tts_ms},
        }

    # ── Face gate ──────────────────────────────────────────────────────────
    # Verify the live camera frame matches the registered reference BEFORE the
    # interview starts. The interviewer never checks this itself, so the start
    # endpoint owns it. Only enforced when face verification is enabled, a
    # reference face exists for this candidate, and Rekognition is available.
    # Fails OPEN on unexpected AWS errors (the per-minute compare loop still
    # guards the session) but CLOSED on a genuine mismatch / missing frame.
    # SKIP face gate for LMS sessions where face was already provided by LMS.
    if ANTICHEAT_FEATURES.get("face_comparison", {}).get("enabled", True):
        ref_b64 = session.get("face_ref_image")
        is_lms_session = session.get("lms_source", False)

        if is_lms_session and ref_b64:
            log.info(f"[FaceGate] Session {sid[:8]}: skipped (LMS-provided face reference)")

        # Skip live camera verification for LMS sessions (face already validated by LMS)
        if ref_b64 and rekognition_client and not is_lms_session:
            live_b64 = data.get("face_image") or ""
            if not live_b64:
                raise HTTPException(428, "Camera is off or no frame was captured. "
                                         "Enable your camera so we can verify your identity, then try again.")
            try:
                _fg_t0 = time.time()
                resp = rekognition_client.compare_faces(
                    SourceImage={"Bytes": base64.b64decode(ref_b64)},
                    TargetImage={"Bytes": base64.b64decode(live_b64)},
                    SimilarityThreshold=0.0,
                )
                # One CompareFaces call per interview start — record its cost.
                session.setdefault("obs_log", []).append(
                    _obs_entry("Rekognition", "aws-rekognition-compare-faces",
                               round((time.time() - _fg_t0) * 1000),
                               cost_usd=_REKOGNITION_COST_PER_IMAGE))
                matches = resp.get("FaceMatches", [])
                similarity = matches[0]["Similarity"] if matches else 0.0
                gate_ok = similarity >= FACE_COMPARE_THRESHOLD
                session.setdefault("anticheat_log", []).append({
                    "event_type": "face_gate",
                    "turn": 0, "timestamp": time.time(),
                    "metadata": f"start similarity={similarity:.1f}% ok={gate_ok}",
                })
                if not gate_ok:
                    log.info(f"[FaceGate] Session {sid[:8]}: start BLOCKED (similarity={similarity:.1f}%)")
                    raise HTTPException(403, "Face verification failed — the person on camera does "
                                             "not match the registered face. Make sure the registered "
                                             "candidate is clearly visible, then try again.")
                log.info(f"[FaceGate] Session {sid[:8]}: start verified (similarity={similarity:.1f}%)")
            except HTTPException:
                raise
            except rekognition_client.exceptions.InvalidParameterException:
                raise HTTPException(422, "No face detected on camera. Make sure your face is clearly "
                                         "visible and well-lit, then try again.")
            except Exception as e:
                log.error(f"[FaceGate] compare failed, allowing start: {e}")

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
    no_response = bool(data.get("no_response", False))
    session = sessions.get(sid)
    if not session: raise HTTPException(404, "Session not found")

    # Check if speaker mismatch was detected in background
    if session.get("speaker_mismatch"):
        _end_interview(session, reason="speaker_verification_failed")
        return {
            "question": "This interview has been ended due to a speaker verification failure.",
            "question_type": "end", "turn": session["turn"], "phase": "ended",
            "audio": "", "difficulty": "basic", "should_end": True,
            "speaker_mismatch": True,
        }

    t0_total = time.time()
    result = generate_question(session, answer, no_response=no_response)

    # If it's a repeat request, use cached audio if available
    is_repeat = result.get("repeat_question", False)
    if is_repeat:
        # Try to reuse audio from last question (if TTS is expensive)
        audio, tts_ms = synthesize_speech(result["question"])
        log.info(f"[RepeatRequest] Repeating question for session {sid[:8]}")
    else:
        audio, tts_ms = synthesize_speech(result["question"])

    # Store TTS timing + cost (skip for repeat to avoid double-counting)
    if not is_repeat:
        tts_provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
        session.setdefault("obs_log", []).append(
            _obs_entry("TTS", tts_provider, tts_ms, "success" if audio else "failure",
                       chars=len(result["question"]), cost_usd=_calc_tts_cost(tts_provider, len(result["question"]))))

    if result["should_end"]:
        # Reason already set in generate_question, don't override
        if "end_reason" not in session:
            _end_interview(session, reason="llm_decision")
        save_candidate_session(session)

    sessions[sid] = session

    # Evaluate after the writeback so the eval thread's jsonb_set isn't clobbered.
    if result["should_end"]:
        _evaluate_async(session)

    total_ms = round((time.time() - t0_total) * 1000)
    llm_ms = result.get("llm_ms", 0)
    log.info(f"[Turn {session['turn']}] Total: {total_ms}ms (LLM: {llm_ms}ms + TTS: {tts_ms}ms)")

    return {
        "question": result["question"], "question_type": "interview",
        "turn": session["turn"], "phase": session["phase"],
        "audio": audio, "difficulty": "basic",
        "should_end": result["should_end"],
        "pause_prompt": result.get("pause_prompt", False),
        "repeat_question": is_repeat,
        "timing": {"llm_ms": llm_ms, "tts_ms": tts_ms, "total_ms": total_ms},
    }


_NUDGE_AUDIO_CACHE = {}

@app.post("/api/nudge-audio")
def nudge_audio(data: dict):
    """Interviewer-voice audio for the no-answer nudge ("please answer the question").
    Voice-only — the UI keeps the current question displayed. Cached per TTS provider
    so a silent candidate doesn't cost a fresh TTS call every time."""
    _sync_runtime_config()
    phrase = "Please answer the question."
    provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
    key = f"{provider}:{phrase}"
    audio = _NUDGE_AUDIO_CACHE.get(key)
    if audio is None:
        audio, _ms = synthesize_speech(phrase)
        _NUDGE_AUDIO_CACHE[key] = audio
    return {"audio": audio, "phrase": phrase}


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
    from starlette.responses import StreamingResponse
    if session.get("speaker_mismatch"):
        _end_interview(session, reason="speaker_verification_failed")
        def mismatch_stream():
            msg = "This interview has been ended due to a speaker verification failure."
            yield f"data: {json.dumps({'type': 'text', 'content': msg, 'done': True, 'should_end': True, 'speaker_mismatch': True})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'turn': session['turn'], 'phase': 'ended', 'speaker_mismatch': True})}\n\n"
        return StreamingResponse(mismatch_stream(), media_type="text/event-stream")

    def event_stream():
        t0 = time.time()

        # Add candidate's answer to history.
        # If the last LLM response was a pause prompt ("go ahead, finish that thought"),
        # append to the existing answer instead of replacing it.
        if session["conversation"]:
            if session.pop("_last_was_pause", False) and session["conversation"][-1].get("answer"):
                session["conversation"][-1]["answer"] += " " + answer
            else:
                session["conversation"][-1]["answer"] = answer
            turn_idx = len(session["conversation"]) - 1
            threading.Thread(target=detect_ai_answer, args=(session["conversation"][-1]["answer"], session, turn_idx), daemon=True).start()

        # Check auto-end
        should_end, end_msg = _should_end_interview(session)
        if should_end:
            _end_interview(session, reason="time_limit")
            audio_bytes = tts_chunk(end_msg)
            sessions[sid] = session
            _evaluate_async(session)  # hard-limit end returns early — evaluate here too
            yield f"data: {json.dumps({'type': 'text', 'content': end_msg, 'done': True, 'should_end': True})}\n\n"
            if audio_bytes:
                yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode()})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'turn': session['turn'], 'phase': 'ended'})}\n\n"
            return

        # Stop agent runs OFF the critical path (background thread, spawned below),
        # so here we only ENFORCE its stashed verdict — instant, no added latency —
        # plus a cheap synchronous hard-max guard so we can never exceed the ceiling.
        _answered = _count_answered(session)
        _sd = session.get("_stop_decision") or {}
        _stop_now = (_answered >= STOP_AGENT_HARD_MAX) or (_answered >= STOP_AGENT_MIN_Q and _sd.get("stop"))
        if _stop_now:
            end_reason = "hard_max" if _answered >= STOP_AGENT_HARD_MAX else "stop_agent"
            _end_interview(session, reason=end_reason)
            reason = "hard_max" if _answered >= STOP_AGENT_HARD_MAX else _sd.get("reason", "")
            log.info(f"[StopAgent] Ending at {_answered} questions — {reason}")
            closing = _stop_agent_closing(session)
            audio_bytes = tts_chunk(closing)
            sessions[sid] = session
            _evaluate_async(session)
            yield f"data: {json.dumps({'type': 'text', 'content': closing, 'done': True, 'should_end': True})}\n\n"
            if audio_bytes:
                yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode()})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'turn': session['turn'], 'phase': 'ended', 'should_end': True})}\n\n"
            return

        # Spawn the stop agent in parallel (off the response path) to decide about
        # the NEXT turn. Its verdict is enforced at the top of the next request.
        if _answered >= STOP_AGENT_MIN_Q:
            threading.Thread(target=_stop_agent_background, args=(session,), daemon=True).start()

        # Build prompt
        messages = build_interview_prompt(session)
        turn = session["turn"]
        phase = _get_interview_phase(turn)
        topics_covered = _get_topics_covered(session)
        pacing = f"\nPHASE: {phase} | Turn: {turn}"
        if topics_covered:
            pacing += f"\nTopics covered: {', '.join(topics_covered)}. Ask about DIFFERENT topics."
        # Ending is owned by the stop agent (server-enforced), NOT the interviewer —
        # so the interviewer must never end on its own. It keeps drilling weak answers
        # with follow-ups; the system classifies question type on its own (no tags).
        pacing += ("\nDo NOT end the interview yourself and do NOT output [END_INTERVIEW] — ending is "
                   "handled for you. Otherwise interview normally: when an answer is vague or hand-wavy "
                   "on something that matters, ask one follow-up to pin it down; otherwise move to a new "
                   "topic. Ask in plain words with no tags or labels.")
        # Hard per-turn length reminder — keeps GPT-4.1-mini / Haiku from rambling.
        pacing += ("\nKeep it short and conversational: a brief acknowledgement, then ONE clear question. "
                   "A scenario may add one short setup sentence. Never stack multiple questions or a long "
                   "multi-part setup into one turn.")
        messages.append({"role": "user", "content": answer + pacing})

        # Stream LLM tokens, buffer into sentences
        t0_llm = time.time()
        full_text = ""
        sentence_buffer = ""
        sentence_count = 0
        total_tts_ms = 0
        total_tts_chars = 0
        qgen_model = RUNTIME_CONFIG["qgen_model"]
        input_tokens_est = _estimate_message_tokens(messages, qgen_model)
        stream_error = False
        _VOICE_ONLY_PATTERNS = ["please answer in english", "answer in english", "speak in english",
                                "please speak in english", "please respond in english"]

        _STRIP_TAGS = ["[FOLLOWUP]", "[SCENARIO]", "[END_INTERVIEW]", "[PERSONAL]", "[ABUSIVE]",
                       "[CONCEPT]", "[PROJECT]"]  # last two: invented labels, strip defensively

        def _clean_for_tts(text):
            for tag in _STRIP_TAGS:
                text = text.replace(tag, "")
            return text.strip()

        def _is_voice_only(text):
            return any(p in text.lower().strip().rstrip('.!') for p in _VOICE_ONLY_PATTERNS)

        try:
            pending_tts = None  # (future, t0_tts, sentence)
            token_hold = []
            voice_only_hold = False
            # Clause-level streaming: break a sentence into clause segments so each is
            # synthesized and shipped as its own audio event (played back-to-back by the
            # client's existing audio queue). First audio starts sooner. Toggle off if
            # the clause boundaries sound choppy.
            _tts_clause_stream = RUNTIME_CONFIG.get("tts_stream_clauses", True)
            _clause_split_done = False  # cap clause splits at ONE per sentence (limits TTS calls/cost)

            def _flush_tts():
                """Wait for pending TTS future and yield audio."""
                nonlocal pending_tts, total_tts_ms, total_tts_chars, sentence_count
                if not pending_tts:
                    return []
                future, t0_t, sent = pending_tts
                pending_tts = None
                audio_bytes = future.result()
                tts_ms = round((time.time() - t0_t) * 1000)
                total_tts_ms += tts_ms
                total_tts_chars += len(sent)
                log.info(f"[Stream] Sentence {sentence_count}: TTS {tts_ms}ms — \"{sent[:50]}...\"")
                if audio_bytes:
                    return [f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode(), 'tts_ms': tts_ms})}\n\n"]
                return []

            for token in stream_llm(messages, temperature=0.7, max_tokens=150):
                full_text += token
                sentence_buffer += token
                token_hold.append(token)

                # Segment boundary — end of sentence (or over-long buffer), OR (when
                # clause-streaming is on) ONE clause break per sentence once the buffer
                # is long enough. Capping at a single split keeps first-audio early while
                # limiting extra TTS calls to at most one per sentence.
                _is_sentence_end = bool(re.search(r'[.?!]\s*$', sentence_buffer))
                _seg_boundary = _is_sentence_end or len(sentence_buffer) > 150
                if _tts_clause_stream and not _seg_boundary and not _clause_split_done \
                        and len(sentence_buffer) >= 30 and re.search(r'[,;:—]\s*$', sentence_buffer):
                    _seg_boundary = True
                if _seg_boundary:
                    _clause_split_done = not _is_sentence_end  # set after a mid-sentence split; reset at sentence end
                    sentence = sentence_buffer.strip()
                    sentence_buffer = ""
                    if not sentence:
                        continue
                    voice_only_hold = _is_voice_only(sentence)
                    if not voice_only_hold:
                        ui_text = _clean_for_tts(sentence)
                        if ui_text:
                            yield f"data: {json.dumps({'type': 'token', 'content': ui_text + ' '})}\n\n"
                    else:
                        log.info(f"[Stream] Voice-only (hidden from UI): \"{sentence[:60]}\"")
                    token_hold = []

                    # Flush previous TTS before starting new one
                    for evt in _flush_tts():
                        yield evt

                    sentence_count += 1
                    tts_sentence = _clean_for_tts(sentence)
                    t0_tts = time.time()
                    if tts_sentence:
                        pending_tts = (_tts_executor.submit(tts_chunk, tts_sentence), t0_tts, tts_sentence)

        except Exception as e:
            stream_error = True
            log.error(f"[Stream] LLM streaming error: {e}")
            session.setdefault("obs_log", []).append(
                _obs_entry("LLM_question", qgen_model, round((time.time() - t0_llm) * 1000), "error", error=str(e)))

        # If LLM returned nothing or errored with no text, send a fallback
        if not full_text.strip():
            fallback = "Could you repeat that? I missed what you said."
            full_text = fallback
            yield f"data: {json.dumps({'type': 'token', 'content': fallback})}\n\n"
            audio_bytes = tts_chunk(fallback)
            if audio_bytes:
                yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode(), 'tts_ms': 0})}\n\n"
            log.error(f"[Stream] Empty LLM response — sent fallback (error={stream_error})")

        # Flush remaining buffer
        if sentence_buffer.strip():
            sentence = sentence_buffer.strip()
            if not _is_voice_only(sentence):
                ui_text = _clean_for_tts(sentence)
                if ui_text:
                    yield f"data: {json.dumps({'type': 'token', 'content': ui_text + ' '})}\n\n"
            token_hold = []
            # Flush any pending TTS first
            for evt in _flush_tts():
                yield evt
            sentence_count += 1
            tts_sentence = _clean_for_tts(sentence)
            t0_tts = time.time()
            audio_bytes = tts_chunk(tts_sentence) if tts_sentence else b""
            tts_ms = round((time.time() - t0_tts) * 1000)
            total_tts_ms += tts_ms
            total_tts_chars += len(sentence)
            if audio_bytes:
                yield f"data: {json.dumps({'type': 'audio', 'data': base64.b64encode(audio_bytes).decode(), 'tts_ms': tts_ms})}\n\n"
        else:
            # Flush last pending TTS
            for evt in _flush_tts():
                yield evt

        llm_ms = round((time.time() - t0_llm) * 1000)
        # Use actual Bedrock usage if available (includes cache info), else estimate
        _bu = _last_stream_bedrock_usage if _last_stream_bedrock_usage else {}
        input_tokens_est = _bu.get("input_tokens") or input_tokens_est
        output_tokens_est = _bu.get("output_tokens") or _estimate_tokens(full_text, qgen_model)
        _cache_read = _bu.get("cache_read_input_tokens", 0)
        _cache_create = _bu.get("cache_creation_input_tokens", 0)
        llm_cost = _calc_llm_cost(qgen_model, input_tokens_est, output_tokens_est, _cache_read, _cache_create)

        # Clean the full text
        question = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', full_text)
        question = re.sub(r'`([^`]+)`', r'\1', question)
        question = re.sub(r'#{1,3}\s*', '', question).strip()

        # Follow-up / scenario / type are decided by the background classifier now,
        # not the main prompt. Strip any stray classification tags the model emits.
        for _t in ("[FOLLOWUP]", "[SCENARIO]", "[CONCEPT]", "[PROJECT]"):
            question = question.replace(_t, "")
        question = question.strip()

        # Check behavior tags
        is_end = False
        if "[PERSONAL]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
            question = question.replace("[PERSONAL]", "").strip()
        elif "[ABUSIVE]" in question and ANTICHEAT_FEATURES.get("behavior_guard", {}).get("enabled", True):
            question = question.replace("[ABUSIVE]", "").strip()
            _end_interview(session, reason="abusive_behavior")
            is_end = True
            if ANTICHEAT_FEATURES.get("abuse_email_alert", {}).get("enabled", True):
                threading.Thread(target=send_abuse_email, args=(session, answer), daemon=True).start()
        elif "[END_INTERVIEW]" in question:
            question = question.replace("[END_INTERVIEW]", "").strip()
            _end_interview(session, reason="llm_decision")
            is_end = True

        # Pause prompts (e.g. "Take your time", "Go ahead, finish that thought") are
        # NOT new questions. Don't bump turn, don't append to history. The candidate's
        # next answer attaches to the ORIGINAL question.
        is_pause_prompt = _is_pause_prompt(question)
        if is_pause_prompt:
            log.info(f"[Stream] Pause prompt detected — not counting as a turn: \"{question}\"")
            session["_last_was_pause"] = True
        else:
            session.pop("_last_was_pause", None)
            session["conversation"].append({"question": question, "answer": None, "turn": session["turn"]})
            session["turn"] += 1
            # Background jobs (off the hot path): expected points for scoring + follow-up
            # steering, and tag classification (follow-up / scenario / type). Both must be
            # spawned here too — this streaming path is what live interviews use.
            if not is_end:
                # Expected points generation removed per user request
                threading.Thread(target=classify_question_tags,
                                 args=(session, question), daemon=True).start()

        # Track LLM cost
        tts_provider = RUNTIME_CONFIG.get("tts_provider", "deepgram")
        tts_cost = _calc_tts_cost(tts_provider, total_tts_chars)
        if not stream_error:
            session.setdefault("obs_log", []).append(
                _obs_entry("LLM_question", qgen_model, llm_ms, "success",
                           input_tokens=input_tokens_est, output_tokens=output_tokens_est, cost_usd=llm_cost,
                           cache_read_tokens=_cache_read, cache_write_tokens=_cache_create))
        # Track TTS cost
        session.setdefault("obs_log", []).append(
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
        log.error(f"[Stream Turn {session['turn']}] Total: {total_ms}ms (LLM: {llm_ms}ms, {sentence_count} TTS chunks, error={stream_error})")

        # Final done event
        yield f"data: {json.dumps({'type': 'done', 'question': question, 'turn': session['turn'], 'phase': session['phase'], 'should_end': is_end, 'pause_prompt': is_pause_prompt, 'timing': {'llm_ms': llm_ms, 'total_ms': total_ms}})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/end-session")
def end_session(data: dict):
    sid = data.get("session_id")
    session = sessions.get(sid)
    if session:
        _end_interview(session, reason="manual")
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
            "has_voice_ref": bool(session.get("user_voice_ref")),
            "has_face_ref": bool(session.get("face_ref_image"))}


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
    pq = evaluation.get("per_question", [])
    if pq:
        pq = _enforce_followup_grouping(session, pq)
    scores = {
        "overall_score": evaluation.get("overall_score"),
        "level_fit": evaluation.get("level_fit"),
        "verdict": evaluation.get("verdict"),
        "communication_score": evaluation.get("communication_score"),
        "communication": evaluation.get("communication"),
        "strengths": evaluation.get("strengths", []),
        "weaknesses": evaluation.get("weaknesses", []),
        "per_question": pq,
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
    "multiple_persons": {
        "label": "Background Person Detection",
        "description": "Detects if another person is visible in the camera using COCO-SSD body detection",
        "category": "camera",
        "enabled": True,
    },
    "face_comparison": {
        "label": "Face ID Verification (AWS)",
        "description": "Periodically compares candidate face against registered reference using AWS Rekognition CompareFaces every 60 seconds",
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
        log.info(f"[Gaze] Loaded {os.path.basename(GAZE_MODEL_PATH)} — "
                 f"model={bundle.get('name', '?')}, features={n_feat}, classes={labels}")
        return bundle
    except Exception as e:
        log.error(f"[Gaze] Failed to load model from {GAZE_MODEL_PATH}: {e}")
        return None


@app.post("/api/get-comparison")
def get_comparison_public(data: dict):
    """
    Public endpoint for candidates to view their progress comparison.
    Returns comparison between current and previous interview(s).
    """
    sid = data.get("session_id", "")
    session = sessions.get(sid)
    if not session and database.is_available():
        session = database.get_active_session(sid)
    if not session:
        raise HTTPException(404, "Session not found")

    # Check if evaluation has comparison already stored
    evaluation = session.get("evaluation", {})
    if evaluation and "comparison" in evaluation:
        return evaluation["comparison"]

    # Generate comparison on-the-fly if not stored
    email = session.get("resume", {}).get("email")
    if not email:
        return {
            "status": "no_email",
            "message": "Session does not have candidate email"
        }

    previous_sessions = get_candidate_previous(email)
    previous_sessions = [s for s in previous_sessions if s.get("session_id") != sid]

    if not previous_sessions:
        return {
            "status": "no_history",
            "message": "This is your first interview. Keep going!"
        }

    try:
        comparison = comparison_analysis.compare_interviews(
            current_session=session,
            previous_sessions=previous_sessions,
            openai_client=openai_client,
            model=RUNTIME_CONFIG.get("eval_model", "gpt-4o-mini")
        )
        return comparison
    except Exception as e:
        log.error(f"[Comparison] Failed to generate public comparison: {e}")
        return {
            "status": "error",
            "message": f"Failed to generate comparison: {str(e)}"
        }


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


# ── LMS Integration API ────────────────────────────────────────────────────

@app.get("/api/lms/interview-results")
def get_lms_interview_results(
    email: str = None,
    session_id: str = None,
    domain: str = None,
    limit: int = 100,
    offset: int = 0
):
    """
    GET endpoint for lms_interview_results view.

    Query parameters:
    - email: Filter by candidate email
    - session_id: Get specific session
    - domain: Filter by domain (Physical Design, Design Verification, Analog Layout)
    - limit: Max results (default 100, max 1000)
    - offset: Pagination offset

    Returns JSON array of interview results with evaluation, anti-cheat, and conversation data.
    """
    # Validate limit
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")

    # Build query
    query = "SELECT * FROM lms_interview_results WHERE 1=1"
    params = []

    if session_id:
        query += " AND session_id = %s"
        params.append(session_id)

    if email:
        query += " AND email = %s"
        params.append(email)

    if domain:
        query += " AND domain = %s"
        params.append(domain)

    # Order by most recent first
    query += " ORDER BY completed_at DESC"

    # Pagination
    query += " LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    try:
        log.info(f"[LMS] Query: {query}")
        log.info(f"[LMS] Params: {params}")
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(params))
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]

        # Convert to JSON-serializable format
        results = []
        for row in rows:
            result = dict(zip(columns, row))
            # Convert datetime to ISO string
            if result.get("started_at"):
                result["started_at"] = result["started_at"].isoformat()
            if result.get("completed_at"):
                result["completed_at"] = result["completed_at"].isoformat()
            results.append(result)

        return {
            "ok": True,
            "count": len(results),
            "limit": limit,
            "offset": offset,
            "results": results
        }

    except Exception as e:
        log.error(f"Error fetching LMS interview results: {e}")
        raise HTTPException(500, f"Database error: {str(e)}")


# ── Face Comparison (AWS Rekognition) ─────────────────────────────────────
FACE_COMPARE_THRESHOLD = 90.0   # minimum similarity % for CompareFaces

@app.get("/api/face/check")
def face_check(email: str = ""):
    """Check if a candidate already has a stored face reference (by email)."""
    if not email:
        return {"ok": True, "has_reference": False}
    face_bytes, confidence, wearing_glasses = database.get_face_reference(email)
    return {
        "ok": True,
        "has_reference": face_bytes is not None,
        "wearing_glasses": wearing_glasses,
    }


@app.post("/api/face/register")
def face_register(data: dict):
    """Register a reference face image (base64 JPEG) from webcam capture.
    Validates with DetectFaces, detects glasses, persists in DB by email."""
    image_b64 = data.get("image", "")
    email = data.get("email", "")
    if not image_b64:
        raise HTTPException(400, "Missing image")
    if not rekognition_client:
        return {"ok": False, "error": "AWS Rekognition not configured — cannot register face"}
    wearing_glasses = False
    try:
        img_bytes = base64.b64decode(image_b64)
        resp = rekognition_client.detect_faces(
            Image={"Bytes": img_bytes},
            Attributes=["ALL"]
        )
        faces = resp.get("FaceDetails", [])
        if len(faces) == 0:
            return {"ok": False, "error": "No face detected in the image"}
        if len(faces) > 1:
            return {"ok": False, "error": "Multiple faces detected — only one person should be visible"}
        face = faces[0]
        # Reject poor-quality captures with actionable guidance so the reference
        # is clear — a dark/blurry reference makes every CompareFaces during the
        # interview unreliable. Rekognition Quality.Brightness/Sharpness are 0-100.
        quality = face.get("Quality", {})
        brightness = quality.get("Brightness", 100)
        sharpness = quality.get("Sharpness", 100)
        face_conf = face.get("Confidence", 100)
        log.info(f"[FaceRegister] Quality — brightness={brightness:.0f} sharpness={sharpness:.0f} conf={face_conf:.0f}")
        if brightness < 30:
            return {"ok": False, "error": "Your face is too dark. Add more light in front of you (avoid sitting with a window or lamp behind you), then capture again."}
        if brightness > 92:
            return {"ok": False, "error": "The image is overexposed by bright light. Move away from the strong light source or dim it, then capture again."}
        if sharpness < 15:
            return {"ok": False, "error": "Your face isn't clear — the image is blurry. Hold still, improve the lighting, and make sure the camera lens is clean, then capture again."}
        if face_conf < 90:
            return {"ok": False, "error": "Your face isn't clearly visible. Face the camera directly in good, even lighting, then capture again."}
        glasses_info = face.get("Eyeglasses", {})
        wearing_glasses = glasses_info.get("Value", False) and glasses_info.get("Confidence", 0) > 80
        log.info(f"[FaceRegister] Glasses detected: {wearing_glasses} (confidence={glasses_info.get('Confidence', 0):.1f}%)")
    except Exception as e:
        log.error(f"[FaceRegister] DetectFaces failed: {e}")
        raise HTTPException(500, f"Face detection failed: {e}")
    # Persist in DB by email (reusable across sessions)
    if email:
        img_bytes = base64.b64decode(image_b64)
        if not database.save_face_reference(email, img_bytes, 0, wearing_glasses=wearing_glasses):
            log.error(f"[FaceRegister] WARNING: failed to save face reference for {email}")
            raise HTTPException(500, "Failed to save face reference")
        log.info(f"[FaceRegister] Reference face persisted for {email} ({len(img_bytes)} bytes, glasses={wearing_glasses})")
    return {"ok": True, "face_registered": True, "wearing_glasses": wearing_glasses}


@app.post("/api/face/detect-glasses")
def face_detect_glasses(data: dict):
    """Detect if the person in the image is wearing glasses. Used for pre-interview check."""
    if not rekognition_client:
        return {"ok": False, "error": "AWS Rekognition not configured"}
    image_b64 = data.get("image", "")
    if not image_b64:
        raise HTTPException(400, "Missing image")
    try:
        img_bytes = base64.b64decode(image_b64)
        resp = rekognition_client.detect_faces(
            Image={"Bytes": img_bytes},
            Attributes=["ALL"]
        )
        faces = resp.get("FaceDetails", [])
        if not faces:
            return {"ok": True, "wearing_glasses": False, "no_face": True}
        glasses_info = faces[0].get("Eyeglasses", {})
        wearing = glasses_info.get("Value", False) and glasses_info.get("Confidence", 0) > 80
        return {"ok": True, "wearing_glasses": wearing, "confidence": round(glasses_info.get("Confidence", 0), 1)}
    except Exception as e:
        log.error(f"[FaceDetectGlasses] Error: {e}")
        return {"ok": False, "error": str(e)}


@app.post("/api/face/compare")
def face_compare(data: dict):
    """Compare a webcam frame against the registered reference face."""
    if not ANTICHEAT_FEATURES.get("face_comparison", {}).get("enabled", True):
        return {"ok": True, "ignored": True, "reason": "feature_disabled"}
    if not rekognition_client:
        raise HTTPException(503, "AWS Rekognition not configured")
    sid = data.get("session_id", "")
    image_b64 = data.get("image", "")
    if not sid or not image_b64:
        raise HTTPException(400, "Missing session_id or image")
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    ref_b64 = session.get("face_ref_image")
    if not ref_b64:
        return {"ok": True, "skipped": True, "reason": "no_reference_face"}
    try:
        ref_bytes = base64.b64decode(ref_b64)
        target_bytes = base64.b64decode(image_b64)
        _fc_t0 = time.time()
        resp = rekognition_client.compare_faces(
            SourceImage={"Bytes": ref_bytes},
            TargetImage={"Bytes": target_bytes},
            SimilarityThreshold=0.0,
        )
        # Recurring per-interview cost: this endpoint is polled ~once a minute.
        session.setdefault("obs_log", []).append(
            _obs_entry("Rekognition", "aws-rekognition-compare-faces",
                       round((time.time() - _fc_t0) * 1000),
                       cost_usd=_REKOGNITION_COST_PER_IMAGE))
        matches = resp.get("FaceMatches", [])
        if matches:
            similarity = matches[0]["Similarity"]
            matched = similarity >= FACE_COMPARE_THRESHOLD
        else:
            similarity = 0.0
            matched = False

        # Detect glasses on the live frame to check for glasses mismatch
        glasses_mismatch = None
        ref_glasses = session.get("face_ref_glasses", False)
        try:
            _dg_t0 = time.time()
            detect_resp = rekognition_client.detect_faces(
                Image={"Bytes": target_bytes},
                Attributes=["ALL"]
            )
            session.setdefault("obs_log", []).append(
                _obs_entry("Rekognition", "aws-rekognition-detect-faces",
                           round((time.time() - _dg_t0) * 1000),
                           cost_usd=_REKOGNITION_COST_PER_IMAGE))
            live_faces = detect_resp.get("FaceDetails", [])
            if live_faces:
                live_glasses_info = live_faces[0].get("Eyeglasses", {})
                live_wearing = live_glasses_info.get("Value", False) and live_glasses_info.get("Confidence", 0) > 80
                if ref_glasses and not live_wearing:
                    glasses_mismatch = "registered_with_glasses"
                elif not ref_glasses and live_wearing:
                    glasses_mismatch = "registered_without_glasses"
        except Exception as ge:
            log.error(f"[FaceCompare] Glasses detection on live frame failed: {ge}")

        # Log anticheat event only if there's a problem (mismatch or glasses issue)
        if not matched or glasses_mismatch:
            session.setdefault("anticheat_log", []).append({
                "event_type": "face_comparison",
                "turn": session.get("turn", 0),
                "timestamp": time.time(),
                "metadata": f"similarity={similarity:.1f}%, matched={matched}, glasses_mismatch={glasses_mismatch}",
            })
            log.warning(f"[FaceCompare] Session {sid[:8]}: ALERT — similarity={similarity:.1f}%, matched={matched}, glasses_mismatch={glasses_mismatch}")
        sessions[sid] = session

        result = {
            "ok": True,
            "matched": matched,
            "similarity": round(similarity, 2),
            "threshold": FACE_COMPARE_THRESHOLD,
            "faces_in_target": len(matches),
        }
        if glasses_mismatch:
            result["glasses_mismatch"] = glasses_mismatch
        return result
    except rekognition_client.exceptions.InvalidParameterException as e:
        # No face in source or target image
        session.setdefault("anticheat_log", []).append({
            "event_type": "face_comparison",
            "turn": session.get("turn", 0),
            "timestamp": time.time(),
            "metadata": f"error=no_face_detected",
        })
        sessions[sid] = session
        return {"ok": True, "matched": False, "similarity": 0, "error": "no_face_detected"}
    except Exception as e:
        log.error(f"[FaceCompare] Error: {e}")
        raise HTTPException(500, f"Face comparison failed: {e}")


# ── Admin: LLM Config ───────────────────────────────────────────────────

AVAILABLE_MODELS = [
    # Fast tier
    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "tier": "fast", "input_cost": "$0.15/1M", "output_cost": "$0.60/1M", "latency": "~1-2s", "context": "128K", "best_for": "Fast, cheap"},
    {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "tier": "fast", "input_cost": "$0.40/1M", "output_cost": "$1.60/1M", "latency": "~1-2s", "context": "1M", "best_for": "Smarter than 4o-mini, 1M context"},
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
    valid_levels = {"trained_fresher", "experienced_junior", "experienced_senior", "fresh_graduate"}
    if domain not in SUPPORTED_DOMAINS or level not in valid_levels:
        raise HTTPException(400, f"Invalid domain or level. Allowed domains: {list(SUPPORTED_DOMAINS.keys())}, levels: {valid_levels}")
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
            {"provider": "inworld", "model": "inworld/inworld-stt-1", "name": "Inworld STT-1", "latency": "~300-800ms", "cost": "$0.15/hr ($0.0025/min)"},
        ],
    }

@app.post("/api/admin/stt-config")
async def set_stt_config(data: dict, _=Depends(require_admin)):
    if "provider" in data:
        RUNTIME_CONFIG["stt_provider"] = data["provider"]
    if "model" in data:
        RUNTIME_CONFIG["stt_model"] = data["model"]
    _persist_runtime_config()
    log.info(f"[STT Config] Changed to {RUNTIME_CONFIG['stt_provider']}/{RUNTIME_CONFIG['stt_model']}")
    return {"status": "success", "provider": RUNTIME_CONFIG["stt_provider"], "model": RUNTIME_CONFIG["stt_model"]}

@app.post("/api/admin/stt-test")
async def stt_test(audio: UploadFile = File(...), provider: str = Form("openai"), model: str = Form("gpt-4o-mini-transcribe"), _=Depends(require_admin)):
    """STT Playground — transcribe audio with a specific provider/model for testing."""
    audio_bytes = await audio.read()
    ext = audio.filename.rsplit(".", 1)[-1] if audio.filename else "webm"
    audio_duration_ms = _get_audio_duration_ms(audio_bytes, ext)

    # Temporarily override the provider/model
    orig_provider = RUNTIME_CONFIG.get("stt_provider")
    orig_model = RUNTIME_CONFIG.get("stt_model")
    RUNTIME_CONFIG["stt_provider"] = provider
    RUNTIME_CONFIG["stt_model"] = model
    try:
        transcript, latency_ms = transcribe_audio(audio_bytes, ext)
    finally:
        RUNTIME_CONFIG["stt_provider"] = orig_provider
        RUNTIME_CONFIG["stt_model"] = orig_model

    cost = _calc_stt_cost(model, audio_duration_ms if audio_duration_ms > 0 else latency_ms)
    return {
        "transcript": transcript,
        "latency_ms": latency_ms,
        "audio_duration_ms": audio_duration_ms,
        "cost_usd": round(cost, 6),
        "provider": provider,
        "model": model,
        "chars": len(transcript),
    }


@app.get("/api/admin/voice-verification")
async def get_voice_verification_config(_=Depends(require_admin)):
    _sync_runtime_config()

    # Get Eagle system info
    eagle_info = {}
    if eagle_available:
        eagle_info = eagle_speaker_verification.get_system_info()

    return {
        "enabled": RUNTIME_CONFIG.get("voice_verification_enabled", True),
        "engine": "eagle" if eagle_available else "resemblyzer",
        "eagle_available": eagle_available,
        "eagle_info": eagle_info
    }

@app.post("/api/admin/voice-verification")
async def set_voice_verification_config(data: dict, _=Depends(require_admin)):
    RUNTIME_CONFIG["voice_verification_enabled"] = bool(data.get("enabled", True))
    _persist_runtime_config()
    log.info(f"[VoiceVerify] {'Enabled' if RUNTIME_CONFIG['voice_verification_enabled'] else 'Disabled'}")
    return {"status": "success", "enabled": RUNTIME_CONFIG["voice_verification_enabled"]}

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


# ── Admin: User Quota Management ─────────────────────────────────────────

@app.get("/api/admin/quota-list")
async def admin_quota_list(_=Depends(require_admin), limit: int = 100):
    """List all user quotas, ordered by usage descending."""
    quotas = database.get_all_user_quotas(limit=limit)
    return {"quotas": quotas}

@app.post("/api/admin/quota-reset")
async def admin_quota_reset(data: dict, _=Depends(require_admin)):
    """Reset a user's quota usage to 0."""
    email = data.get("email", "").strip()
    if not email:
        raise HTTPException(400, "Email is required")

    database.admin_reset_user_quota(email)
    quota = database.get_user_quota(email)
    log.info(f"[Admin] Reset quota for {email}")
    return {"status": "success", "message": f"Quota reset for {email}", "quota": quota}

@app.post("/api/admin/quota-set-limit")
async def admin_quota_set_limit(data: dict, _=Depends(require_admin)):
    """Set custom quota limit for a user."""
    email = data.get("email", "").strip()
    limit_minutes = data.get("limit_minutes")

    if not email:
        raise HTTPException(400, "Email is required")
    if limit_minutes is None or limit_minutes < 0:
        raise HTTPException(400, "Valid limit_minutes is required (>= 0)")

    database.admin_set_user_quota_limit(email, limit_minutes)
    quota = database.get_user_quota(email)
    log.info(f"[Admin] Set quota limit for {email} to {limit_minutes} minutes")
    return {"status": "success", "message": f"Quota limit set to {limit_minutes} minutes for {email}", "quota": quota}


# ── Admin: Sessions ──────────────────────────────────────────────────────

def _build_pq_by_num(evaluation: dict) -> dict:
    """Build a mapping from question number to per_question eval entry.
    Maps both main question numbers and their follow-up question numbers
    to the same parent entry so grouped scores apply correctly."""
    pq_by_num = {}
    for item in evaluation.get("per_question", []) or []:
        try:
            pq_by_num[int(item.get("q"))] = item
            for fq in item.get("followup_qs", []) or []:
                try:
                    pq_by_num[int(fq)] = item
                except (TypeError, ValueError):
                    pass
        except (TypeError, ValueError):
            continue
    return pq_by_num


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
        pq_by_num = _build_pq_by_num(evaluation)
        scores = [item.get("score") for item in (evaluation.get("per_question", []) or []) if isinstance(item.get("score"), (int, float))]
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
        
        # Calculate duration
        duration_minutes = s.get("duration_minutes")
        if duration_minutes is None and s.get("started_at"):
            end_time = s.get("ended_at", time.time())
            duration_minutes = round((end_time - s["started_at"]) / 60, 2)

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
            "duration_minutes": duration_minutes,
        })
    session_list.sort(key=lambda s: s.get("started_at", 0), reverse=True)
    return session_list


# Preferred display order for cost/latency breakdowns. Any step present in the
# logs but not listed here (e.g. a newly-added cost source) is still shown,
# appended alphabetically — so the admin cost view never silently drops a step.
_OBS_STEP_ORDER = [
    "LLM_greeting", "LLM_question", "LLM_expected_points", "LLM_tag_classify",
    "LLM_ai_detect", "LLM_stop_agent", "LLM_evaluation",
    "STT", "TTS", "TTS_greeting", "Rekognition",
]

def _ordered_obs_steps(logs):
    """Every distinct step in the logs, known ones first (in display order),
    then any unknown steps alphabetically. Keeps the breakdown complete."""
    seen = {l.get("step") for l in logs if l.get("step")}
    known = [s for s in _OBS_STEP_ORDER if s in seen]
    extra = sorted(s for s in seen if s not in _OBS_STEP_ORDER)
    return known + extra


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
    for step in _ordered_obs_steps(logs):
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

@app.post("/api/admin/rerun-eval/{sid}")
def admin_rerun_eval(sid: str, _=Depends(require_admin)):
    """Re-run evaluation for a session, clearing any previous result."""
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    # Clear previous evaluation so it re-runs
    session.pop("evaluation", None)
    if sid in _eval_locks:
        del _eval_locks[sid]
    sessions[sid] = session
    # Run evaluation
    result = evaluate_interview(session)
    return {"status": result.get("status"), "score": result.get("overall_score"),
            "recommendation": result.get("recommendation")}


@app.get("/api/admin/session/{sid}")
def admin_session_detail(sid: str, _=Depends(require_admin)):
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    # Map per-question eval scores back onto turns. evaluate_interview numbers the
    # transcript [Q1], [Q2]... over conversation entries that have a question, so we
    # reproduce that exact counter here and look up each item by its "q" number.
    # Use a copy so we don't mutate session state on read.
    evaluation = dict(session.get("evaluation", {}) or {})
    if evaluation.get("per_question"):
        evaluation["per_question"] = _enforce_followup_grouping(session, evaluation["per_question"])
    pq_by_num = _build_pq_by_num(evaluation)

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

    # Build set of follow-up question numbers and their parent mappings
    followup_nums = set()
    followup_parent = {}  # followup_q_num -> parent_q_num
    for item in evaluation.get("per_question", []) or []:
        parent_q = item.get("q")
        for fq in item.get("followup_qs", []) or []:
            try:
                followup_nums.add(int(fq))
                followup_parent[int(fq)] = int(parent_q)
            except (TypeError, ValueError):
                pass

    turn_log = []
    qnum = 0
    qnum_to_turn = {}
    for entry in session.get("conversation", []):
        has_q = bool((entry.get("question") or "").strip())
        if has_q:
            qnum += 1
            qnum_to_turn[qnum] = entry.get("turn", 0)
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

        parent_qnum = followup_parent.get(qnum)
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
            "missing_points": (pq or {}).get("missing_points", []),
            "level_gap": 0,
            "behavioral_flags": [],
            "ai_detection": entry.get("ai_detection", {}),
            "is_followup": qnum in followup_nums,
            "followup_of": qnum_to_turn.get(parent_qnum) if parent_qnum else None,
        })

    # Calculate trajectory (use per_question directly to avoid duplicates from follow-up mapping)
    scores = [item.get("score") for item in (evaluation.get("per_question", []) or []) if isinstance(item.get("score"), (int, float))]
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


# ── Share Links (read-only review access) ────────────────────────────────

@app.post("/api/admin/share-link")
async def create_share_link(data: dict, request: Request, _=Depends(require_admin)):
    """Generate a read-only share link for a session's expert review."""
    sid = data.get("session_id")
    if not sid:
        raise HTTPException(400, "session_id required")
    session = sessions.get(sid)
    if not session:
        raise HTTPException(404, "Session not found")
    expiry_hours = data.get("expiry_hours", 72)  # default 3 days
    token = jwt.encode({
        "type": "share",
        "sid": sid,
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=expiry_hours),
    }, JWT_SECRET, algorithm="HS256")
    host = request.headers.get("host", request.base_url.hostname)
    scheme = request.headers.get("x-forwarded-proto", "https")
    url = f"{scheme}://{host}/review/{token}"
    log.info(f"[Share] Created share link for session {sid[:8]} (expires in {expiry_hours}h)")
    return {"url": url, "token": token, "expires_hours": expiry_hours}


@app.get("/api/shared/session/{token}")
async def get_shared_session(token: str):
    """Public endpoint: return session detail for a valid share token (read-only)."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != "share":
            raise HTTPException(403, "Invalid share token")
    except JWTError:
        raise HTTPException(403, "Share link expired or invalid")

    sid = payload["sid"]
    session = sessions.get(sid)
    if not session and database.is_available():
        session = database.get_active_session(sid)
    if not session:
        raise HTTPException(404, "Session not found")

    # Reuse admin session detail logic but strip sensitive fields.
    # Copy so we don't mutate session state on read.
    evaluation = dict(session.get("evaluation", {}) or {})
    if evaluation.get("per_question"):
        evaluation["per_question"] = _enforce_followup_grouping(session, evaluation["per_question"])
    pq_by_num = _build_pq_by_num(evaluation)

    # Build follow-up tracking
    followup_nums_lms = set()
    followup_parent_lms = {}
    for item in evaluation.get("per_question", []) or []:
        parent_q = item.get("q")
        for fq in item.get("followup_qs", []) or []:
            try:
                followup_nums_lms.add(int(fq))
                followup_parent_lms[int(fq)] = int(parent_q)
            except (TypeError, ValueError):
                pass

    turn_log = []
    qnum = 0
    qnum_to_turn_lms = {}
    for entry in session.get("conversation", []):
        has_q = bool((entry.get("question") or "").strip())
        if has_q:
            qnum += 1
            qnum_to_turn_lms[qnum] = entry.get("turn", 0)
        pq = pq_by_num.get(qnum) if has_q else None

        parent_qnum = followup_parent_lms.get(qnum)
        turn_log.append({
            "turn": entry.get("turn", 0),
            "question": entry.get("question", ""),
            "answer": entry.get("answer", ""),
            "topic": entry.get("topic", ""),
            "score": (pq or {}).get("score", ""),
            "notes": (pq or {}).get("comment", ""),
            "missing_points": (pq or {}).get("missing_points", []),
            "is_followup": qnum in followup_nums_lms,
            "followup_of": qnum_to_turn_lms.get(parent_qnum) if parent_qnum else None,
        })

    resume = session.get("resume", {}) or {}
    return {
        "session_id": sid,
        "candidate_name": resume.get("candidate_name", "Candidate"),
        "domain": resume.get("domain", ""),
        "level": resume.get("level", ""),
        "turn": session.get("turn", 0),
        "phase": session.get("phase", ""),
        "turn_log": turn_log,
        "evaluation": {
            "overall_score": evaluation.get("overall_score"),
            "recommendation": evaluation.get("recommendation"),
            "level_fit": evaluation.get("level_fit"),
            "verdict": evaluation.get("verdict"),
            "communication_score": evaluation.get("communication_score"),
            "communication": evaluation.get("communication"),
            "strengths": evaluation.get("strengths", []),
            "weaknesses": evaluation.get("weaknesses", []),
            "topic_breakdown": evaluation.get("topic_breakdown", []),
            "summary": evaluation.get("summary", ""),
            "per_question": evaluation.get("per_question", []),
        },
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
    for step in _ordered_obs_steps(all_logs):
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
    review_id = f"R-{secrets.token_hex(4).upper()}"
    session_id = data.get("session_id", "")
    turn_number = int(data.get("turn_number", 0))

    session_data = database.get_active_session(session_id) if session_id else None
    ai_score = float(data.get("ai_score", 0))
    if ai_score == 0 and session_data:
        ev = session_data.get("evaluation", {})
        per_q = ev.get("per_question_scores") or ev.get("per_question", [])
        if isinstance(per_q, list) and turn_number < len(per_q):
            q_data = per_q[turn_number]
            if isinstance(q_data, dict):
                ai_score = float(q_data.get("score") or q_data.get("rating") or 0)

    ok = database.save_expert_review(
        review_id=review_id,
        session_id=session_id,
        turn_number=turn_number,
        reviewer_name=data.get("reviewer_name", "unknown"),
        reviewer_domain=data.get("reviewer_domain", ""),
        reviewer_expertise=data.get("reviewer_expertise", ""),
        ai_score=ai_score,
        human_score=float(data.get("human_score", 0)),
        verdict=data.get("verdict", ""),
        feedback=data.get("feedback", ""),
        error_flags=data.get("error_flags"),
    )
    return {"ok": ok, "review_id": review_id}


@app.get("/api/admin/reviews")
def list_reviews(limit: int = 100, _=Depends(require_admin)):
    return database.list_expert_reviews(limit=limit)


@app.get("/api/admin/reviews/{session_id}")
def get_session_reviews(session_id: str, _=Depends(require_admin)):
    return database.list_expert_reviews(session_id=session_id)


# ── Comparison Analysis ──────────────────────────────────────────────────

@app.get("/api/comparison/{session_id}")
async def get_comparison_analysis(session_id: str, _=Depends(require_admin)):
    """
    Compare a session with the candidate's previous interview(s).
    Returns detailed analysis of improvements and areas still lagging.
    """
    # Get current session
    session = sessions.get(session_id)
    if not session and database.is_available():
        session = database.get_active_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Get candidate email
    email = session.get("resume", {}).get("email")
    if not email:
        raise HTTPException(400, "Session does not have candidate email")

    # Get previous sessions
    previous_sessions = get_candidate_previous(email)

    # Filter out the current session from history if it exists
    previous_sessions = [s for s in previous_sessions if s.get("session_id") != session_id]

    if not previous_sessions:
        return {
            "status": "no_history",
            "message": "This is the candidate's first interview. No comparison available."
        }

    # Perform comparison
    try:
        comparison = comparison_analysis.compare_interviews(
            current_session=session,
            previous_sessions=previous_sessions,
            openai_client=openai_client,
            model=RUNTIME_CONFIG.get("eval_model", "gpt-4o-mini")
        )
        log.info(f"[Comparison] Generated analysis for session {session_id[:8]}, candidate: {email}")
        return comparison
    except Exception as e:
        log.error(f"[Comparison] Failed to generate analysis: {e}")
        raise HTTPException(500, f"Failed to generate comparison: {str(e)}")


@app.get("/api/comparison/{session_id}/report")
async def get_comparison_report(session_id: str, _=Depends(require_admin)):
    """
    Get a text-based comparison report for download or display.
    """
    # Get current session
    session = sessions.get(session_id)
    if not session and database.is_available():
        session = database.get_active_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Get candidate email
    email = session.get("resume", {}).get("email")
    if not email:
        raise HTTPException(400, "Session does not have candidate email")

    # Get previous sessions
    previous_sessions = get_candidate_previous(email)
    previous_sessions = [s for s in previous_sessions if s.get("session_id") != session_id]

    if not previous_sessions:
        report_text = "This is the candidate's first interview. No comparison available."
    else:
        try:
            comparison = comparison_analysis.compare_interviews(
                current_session=session,
                previous_sessions=previous_sessions,
                openai_client=openai_client,
                model=RUNTIME_CONFIG.get("eval_model", "gpt-4o-mini")
            )
            report_text = comparison_analysis.generate_comparison_report_text(comparison)
        except Exception as e:
            log.error(f"[Comparison] Failed to generate report: {e}")
            report_text = f"Error generating comparison report: {str(e)}"

    return Response(
        content=report_text,
        media_type="text/plain",
        headers={
            "Content-Disposition": f"attachment; filename=comparison_report_{session_id[:8]}.txt"
        }
    )


@app.post("/api/comparison/by-email")
async def get_comparison_by_email(data: dict, _=Depends(require_admin)):
    """
    Get comparison analysis for a candidate by email.
    Returns the most recent interview comparison.

    Request: {"email": "candidate@example.com"}
    """
    email = data.get("email", "").strip()
    if not email:
        raise HTTPException(400, "Email is required")

    # Get all sessions for this candidate
    previous_sessions = get_candidate_previous(email)

    if not previous_sessions:
        return {
            "status": "no_history",
            "message": f"No interview history found for {email}"
        }

    if len(previous_sessions) < 2:
        return {
            "status": "no_comparison",
            "message": f"Only one interview found for {email}. Need at least 2 interviews for comparison.",
            "interview_count": 1,
            "latest_session": previous_sessions[0]
        }

    # Get the most recent (current) session
    latest_summary = previous_sessions[-1]
    latest_session_id = latest_summary.get("session_id")

    # Try to load the full session data
    current_session = sessions.get(latest_session_id)
    if not current_session and database.is_available():
        current_session = database.get_active_session(latest_session_id)

    if not current_session:
        # If session not in active_sessions, reconstruct from summary
        return {
            "status": "session_archived",
            "message": f"Latest session {latest_session_id[:8]} is archived. Comparison not available.",
            "latest_session": latest_summary,
            "interview_count": len(previous_sessions)
        }

    # Get previous sessions (all except the latest)
    prev_sessions = previous_sessions[:-1]

    # Perform comparison
    try:
        comparison = comparison_analysis.compare_interviews(
            current_session=current_session,
            previous_sessions=prev_sessions,
            openai_client=openai_client,
            model="gpt-4o-mini"  # Use OpenAI model for comparison
        )
        comparison["email"] = email
        comparison["interview_count"] = len(previous_sessions)
        log.info(f"[Comparison] Generated analysis by email for {email}, {len(previous_sessions)} total interviews")
        return comparison
    except Exception as e:
        log.error(f"[Comparison] Failed to generate analysis for {email}: {e}")
        raise HTTPException(500, f"Failed to generate comparison: {str(e)}")


@app.post("/api/lms/comparison")
async def lms_get_comparison(request: Request, data: dict):
    """
    LMS endpoint to get comparison analysis by email.
    Uses X-API-Key header for authentication (same as /api/lms/start).

    Request:
    {
      "email": "candidate@example.com"
    }
    """
    # Validate LMS API key
    api_key = request.headers.get("X-API-Key", "")
    if not LMS_API_KEY or api_key != LMS_API_KEY:
        raise HTTPException(401, "Invalid or missing API key")

    email = data.get("email", "").strip()
    if not email:
        raise HTTPException(400, "Email is required")

    # Get all sessions for this candidate
    previous_sessions = get_candidate_previous(email)

    if not previous_sessions:
        return {
            "status": "no_history",
            "message": f"No interview history found for {email}",
            "email": email,
            "interview_count": 0
        }

    if len(previous_sessions) < 2:
        return {
            "status": "no_comparison",
            "message": f"Only one interview found for {email}. Need at least 2 interviews for comparison.",
            "email": email,
            "interview_count": 1,
            "latest_session": previous_sessions[0]
        }

    # Get the most recent (current) session
    latest_summary = previous_sessions[-1]
    latest_session_id = latest_summary.get("session_id")

    # Try to load the full session data
    current_session = sessions.get(latest_session_id)
    if not current_session and database.is_available():
        current_session = database.get_active_session(latest_session_id)

    if not current_session:
        # If session not in active_sessions, return basic info
        return {
            "status": "session_archived",
            "message": f"Latest session {latest_session_id[:8]} is archived. Full comparison not available.",
            "email": email,
            "interview_count": len(previous_sessions),
            "latest_session": latest_summary,
            "score_history": [s.get("evaluation", {}).get("overall_score", 0) for s in previous_sessions]
        }

    # Get previous sessions (all except the latest)
    prev_sessions = previous_sessions[:-1]

    # Perform comparison
    try:
        comparison = comparison_analysis.compare_interviews(
            current_session=current_session,
            previous_sessions=prev_sessions,
            openai_client=openai_client,
            model="gpt-4o-mini"  # Use OpenAI model for comparison
        )
        comparison["email"] = email
        comparison["interview_count"] = len(previous_sessions)
        log.info(f"[LMS-Comparison] Generated for {email}, {len(previous_sessions)} total interviews")
        return comparison
    except Exception as e:
        log.error(f"[LMS-Comparison] Failed for {email}: {e}")
        raise HTTPException(500, f"Failed to generate comparison: {str(e)}")


# ── Admin: Cognition AI (DISABLED) ─────────────────────────────────────
# cognition agent disabled — all endpoints commented out
# @app.get("/api/admin/cognition/signals")
# @app.get("/api/admin/cognition/summary")
# @app.get("/api/admin/cognition/diagnoses")
# @app.post("/api/admin/cognition/diagnoses/{diagnosis_id}")
# @app.post("/api/admin/cognition/trigger")


# ── Start ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    log.info(f"  LLM: {RUNTIME_CONFIG['qgen_model']}")
    log.info(f"  TTS: {RUNTIME_CONFIG['tts_provider']} / {RUNTIME_CONFIG['tts_voice']}")
    log.info(f"  STT: gpt-4o-mini-transcribe")
    log.info(f"  Bedrock: {'ready' if bedrock_client else 'not configured'}")
    log.info(f"  Grok: {'ready' if xai_client else 'not configured'}")
    log.info(f"  Redis cache: {'ready' if redis_cache.is_available() else 'not configured'}")
    # 2 workers to match the 2-core box. 4 workers oversubscribed CPU and, at
    # ~250MB RSS each, exhausted the 2GB RAM (with RAG + realtime + Postgres +
    # Redis co-located) → swapping → interview lag. Raise this only alongside a
    # bigger instance.
    uvicorn.run("main:app", host="0.0.0.0", port=8001, workers=2)
