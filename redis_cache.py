"""
Redis cache for active sessions.

Sits in front of the Postgres active_sessions table to cut the per-turn DB
round-trips (every turn reads and writes the whole session blob). Postgres stays
the source of truth; Redis is best-effort — if it is unset or unreachable the app
falls back to Postgres transparently, mirroring database.is_available().
"""
import json
from secrets_proxy import get_secret

REDIS_URL = get_secret("REDIS_URL")
SESSION_TTL = int(get_secret("REDIS_SESSION_TTL", "7200"))  # seconds; refreshed on every write
_KEY = "sess:"

_client = None
_available = False


def init_cache():
    """Connect to Redis. Called once per worker at startup. No-op if REDIS_URL unset."""
    global _client, _available
    if not REDIS_URL:
        print("[Cache] REDIS_URL not set — running without Redis cache")
        return
    try:
        import redis
        _client = redis.from_url(
            REDIS_URL, decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2,
        )
        _client.ping()
        _available = True
        print("[Cache] Redis connected — active-session cache enabled")
    except ImportError:
        print("[Cache] redis not installed — pip install redis (running without cache)")
    except Exception as e:
        print(f"[Cache] Redis connection failed: {e} — running without cache")


def is_available():
    return _available


def get_session(session_id):
    """Return the cached session dict, or None on miss / error."""
    if not _available:
        return None
    try:
        raw = _client.get(_KEY + session_id)
        return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[Cache] get failed: {e}")
        return None


def set_session(session_id, data):
    """Write-through the full session blob with a refreshed TTL."""
    if not _available:
        return
    try:
        _client.set(_KEY + session_id, json.dumps(data, default=str), ex=SESSION_TTL)
    except Exception as e:
        print(f"[Cache] set failed: {e}")


def delete_session(session_id):
    """Drop the cached blob. Called on end/delete and after any targeted jsonb_set
    write to Postgres, so the next read repopulates from the authoritative row."""
    if not _available:
        return
    try:
        _client.delete(_KEY + session_id)
    except Exception as e:
        print(f"[Cache] delete failed: {e}")


# ── Shared key/value JSON (no TTL) — used for cross-worker runtime config ──

def get_json(key):
    """Read a persistent JSON value (e.g. shared runtime config). None on miss/error."""
    if not _available:
        return None
    try:
        raw = _client.get(key)
        return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[Cache] get_json({key}) failed: {e}")
        return None


def set_json(key, data):
    """Write a persistent JSON value with no expiry."""
    if not _available:
        return
    try:
        _client.set(key, json.dumps(data, default=str))
    except Exception as e:
        print(f"[Cache] set_json({key}) failed: {e}")
