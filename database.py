import logging
log = logging.getLogger('interview')
"""
VLSI Interview Platform — PostgreSQL Database Module
"""
import os
from contextlib import contextmanager
from secrets_proxy import get_secret

DATABASE_URL = get_secret("DATABASE_URL")

_pool = None
_db_available = False

# Constant key for the advisory lock that serializes schema creation across workers.
_SCHEMA_LOCK_KEY = 192837465


def init_db():
    """Initialize connection pool and create tables. Called once per worker at startup."""
    global _pool, _db_available
    if not DATABASE_URL:
        log.info("[DB] DATABASE_URL not set — running without database (in-memory only)")
        return

    # Step 1 — connect. DB availability hinges ONLY on a working connection, never on
    # whether THIS worker won the race to create the schema (see step 2).
    try:
        import psycopg
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=10
        )
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        _db_available = True
    except ImportError:
        log.info("[DB] psycopg not installed — pip install psycopg[binary] psycopg_pool")
        return
    except Exception as e:
        log.error(f"[DB] PostgreSQL connection failed: {e}")
        log.info("[DB] Running without database (in-memory only)")
        return

    # Step 2 — create tables. With 4 workers booting together, concurrent
    # CREATE TABLE IF NOT EXISTS races on the system catalogs and raises in all but one
    # worker (e.g. 'duplicate key value violates unique constraint pg_type_typname_nsp_index').
    # Serialize the DDL with a transaction-scoped advisory lock so exactly one worker builds
    # the schema while the others wait and then no-op. A schema hiccup must NOT flip the DB
    # to unavailable — the connection above already proved it works.
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if os.path.exists(schema_path):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
                    with open(schema_path, "r", encoding="utf-8") as f:
                        cur.execute(f.read())
                conn.commit()
            log.info("[DB] PostgreSQL connected and schema ready")
        except Exception as e:
            log.info(f"[DB] schema init skipped ({e}) — assuming another worker created it")
    else:
        log.info("[DB] PostgreSQL connected (no schema.sql found)")


def is_available():
    return _db_available


@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        # Reads use psycopg's default (autocommit off), so a plain SELECT opens an
        # implicit transaction the callers never close. Roll it back here so the
        # connection returns to the pool IDLE instead of INTRANS/INERROR — otherwise
        # the pool rolls it back itself and logs a noisy warning on every read.
        # Writers commit before this runs, leaving the connection IDLE (a no-op here).
        try:
            if conn.info.transaction_status != 0:  # 0 == TransactionStatus.IDLE
                conn.rollback()
        except Exception:
            pass
        _pool.putconn(conn)


def get_or_create_candidate(candidate_key, candidate_name, domain="physical_design",
                            level="unknown", years_experience=0, expertise=None,
                            tools=None, education=""):
    """Returns candidate DB id. Creates if not exists, updates if exists."""
    if not _db_available:
        return None
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    INSERT INTO candidates (candidate_key, candidate_name, domain, level,
                                            years_experience, expertise, tools, education)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (candidate_key) DO UPDATE SET
                        candidate_name = EXCLUDED.candidate_name,
                        domain = EXCLUDED.domain,
                        level = EXCLUDED.level,
                        years_experience = EXCLUDED.years_experience,
                        expertise = EXCLUDED.expertise,
                        tools = EXCLUDED.tools,
                        education = EXCLUDED.education,
                        updated_at = NOW()
                    RETURNING id
                """, (candidate_key, candidate_name, domain, level, years_experience,
                      expertise or [], tools or [], education))
                conn.commit()
                return cur.fetchone()["id"]
    except Exception as e:
        log.error(f"[DB] get_or_create_candidate failed: {e}")
        return None


def save_active_session(session_id, session_data):
    """Save active session state (JSON data) to PostgreSQL."""
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO active_sessions (session_id, session_data, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        session_data = EXCLUDED.session_data,
                        updated_at = NOW()
                """, (session_id, json.dumps(session_data)))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] save_active_session failed: {e}")
        return False


def get_active_session(session_id):
    """Load active session state from PostgreSQL."""
    if not _db_available:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_data FROM active_sessions WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
                if row:
                    data = row[0]
                    if isinstance(data, str):
                        import json
                        return json.loads(data)
                    return data
                return None
    except Exception as e:
        log.error(f"[DB] get_active_session failed: {e}")
        return None


def active_session_exists(session_id):
    """Check if active session exists in PostgreSQL."""
    if not _db_available:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM active_sessions WHERE session_id = %s", (session_id,))
                return cur.fetchone() is not None
    except Exception as e:
        log.error(f"[DB] active_session_exists failed: {e}")
        return False


def list_active_sessions():
    """List all active sessions."""
    if not _db_available:
        return []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_data FROM active_sessions ORDER BY updated_at DESC")
                rows = cur.fetchall()
                sessions_list = []
                for row in rows:
                    data = row[0]
                    if isinstance(data, str):
                        import json
                        data = json.loads(data)
                    sessions_list.append(data)
                return sessions_list
    except Exception as e:
        log.error(f"[DB] list_active_sessions failed: {e}")
        return []


def list_ended_sessions_needing_eval(grace_sec: int = 120, limit: int = 50):
    """Ended sessions with no evaluation, untouched for at least grace_sec.
    The grace window keeps the sweeper from racing the foreground end-handler that
    just spawned its own eval thread."""
    if not _db_available:
        return []
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_data FROM active_sessions
                    WHERE session_data->>'phase' = 'ended'
                      AND (session_data->'evaluation' IS NULL
                           OR session_data->'evaluation' = 'null'::jsonb)
                      AND updated_at < NOW() - (%s || ' seconds')::interval
                    ORDER BY updated_at ASC
                    LIMIT %s
                """, (str(grace_sec), limit))
                out = []
                for (data,) in cur.fetchall():
                    if isinstance(data, str):
                        data = json.loads(data)
                    out.append(data)
                return out
    except Exception as e:
        log.error(f"[DB] list_ended_sessions_needing_eval failed: {e}")
        return []


def list_active_session_keys():
    """List all active session keys."""
    if not _db_available:
        return []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_id FROM active_sessions")
                return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] list_active_session_keys failed: {e}")
        return []


def delete_active_session(session_id):
    """Remove an active session row. Called when an interview ends so the
    active_sessions table doesn't grow unbounded. Idempotent."""
    if not _db_available:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM active_sessions WHERE session_id = %s", (session_id,))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] delete_active_session failed: {e}")
        return False


def update_session_ai_detection(session_id, turn_index, detection):
    """Merge an ai_detection result into a single conversation turn via jsonb_set.
    A background thread calls this, so it must NOT rewrite the whole session blob —
    a full read-modify-write would race the foreground turn handler (lost updates,
    and json.dumps tearing on a concurrently-mutated dict). jsonb_set updates just
    the one path atomically in the DB."""
    if not _db_available:
        return False
    try:
        import json
        path = ["conversation", str(turn_index), "ai_detection"]
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE active_sessions
                    SET session_data = jsonb_set(session_data, %s::text[], %s::jsonb, true),
                        updated_at = NOW()
                    WHERE session_id = %s
                """, (path, json.dumps(detection), session_id))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] update_session_ai_detection failed: {e}")
        return False


def update_session_question_tags(session_id, turn_index, is_followup, is_scenario, qtype):
    """Atomically write tag-classifier results onto a single conversation turn.
    Uses jsonb_set so it never races with the foreground turn handler."""
    if not _db_available:
        return False
    try:
        import json
        # Build the patch by chaining jsonb_set calls at the SQL level.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE active_sessions
                    SET session_data = jsonb_set(
                        jsonb_set(
                            jsonb_set(session_data,
                                %s::text[], %s::jsonb, true),
                            %s::text[], %s::jsonb, true),
                        %s::text[], %s::jsonb, true),
                        updated_at = NOW()
                    WHERE session_id = %s
                """, (
                    ["conversation", str(turn_index), "is_followup"], json.dumps(is_followup),
                    ["conversation", str(turn_index), "is_scenario"], json.dumps(is_scenario),
                    ["conversation", str(turn_index), "qtype"], json.dumps(qtype),
                    session_id,
                ))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] update_session_question_tags failed: {e}")
        return False


def update_session_expected_points(session_id, turn_index, points):
    """Atomically write expected_points onto a single conversation turn via jsonb_set."""
    if not _db_available:
        return False
    try:
        import json
        path = ["conversation", str(turn_index), "expected_points"]
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE active_sessions
                    SET session_data = jsonb_set(session_data, %s::text[], %s::jsonb, true),
                        updated_at = NOW()
                    WHERE session_id = %s
                """, (path, json.dumps(points), session_id))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] update_session_expected_points failed: {e}")
        return False


def append_session_obs_log(session_id, entry):
    """Atomically append one obs_log entry to a session via jsonb array append."""
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE active_sessions
                    SET session_data = jsonb_set(
                        session_data,
                        '{obs_log}',
                        COALESCE(session_data->'obs_log', '[]'::jsonb) || %s::jsonb,
                        true),
                        updated_at = NOW()
                    WHERE session_id = %s
                """, (json.dumps([entry]), session_id))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] append_session_obs_log failed: {e}")
        return False


def get_app_config(key):
    """Read shared app config (e.g. runtime LLM/TTS/STT settings). None on miss."""
    if not _db_available:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT config_value FROM app_config WHERE config_key = %s", (key,))
                row = cur.fetchone()
                if row:
                    val = row[0]
                    if isinstance(val, str):
                        import json
                        return json.loads(val)
                    return val
                return None
    except Exception as e:
        log.error(f"[DB] get_app_config failed: {e}")
        return None


def save_app_config(key, value):
    """Upsert shared app config. Durable source of truth across workers/restarts."""
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO app_config (config_key, config_value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (config_key) DO UPDATE SET
                        config_value = EXCLUDED.config_value,
                        updated_at = NOW()
                """, (key, json.dumps(value)))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] save_app_config failed: {e}")
        return False


def save_session_evaluation(session_id, evaluation):
    """Merge the end-of-interview evaluation into the session blob via jsonb_set.
    The evaluation runs in a background thread, so it targets ONLY the top-level
    'evaluation' key rather than rewriting the whole blob — a full read-modify-write
    would race the foreground turn handler and the ai_detection writer."""
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE active_sessions
                    SET session_data = jsonb_set(session_data, '{evaluation}', %s::jsonb, true),
                        updated_at = NOW()
                    WHERE session_id = %s
                """, (json.dumps(evaluation), session_id))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] save_session_evaluation failed: {e}")
        return False


def append_session_obs(session_id, entry):
    """Append one observability entry to the session's obs_log via jsonb concat.
    Each UPDATE takes the row lock, so concurrent appends serialize and none are
    lost — unlike a full-blob writeback, which would clobber the rest of the session."""
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE active_sessions
                    SET session_data = jsonb_set(
                            session_data, '{obs_log}',
                            COALESCE(session_data->'obs_log', '[]'::jsonb) || %s::jsonb, true),
                        updated_at = NOW()
                    WHERE session_id = %s
                """, (json.dumps([entry]), session_id))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] append_session_obs failed: {e}")
        return False


def save_candidate_history(email, session_id, session_summary):
    """Save a single session summary to candidate history in PostgreSQL."""
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO candidate_history (email, session_id, session_summary, created_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        email = EXCLUDED.email,
                        session_summary = EXCLUDED.session_summary
                """, (email, session_id, json.dumps(session_summary)))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] save_candidate_history failed: {e}")
        return False


def get_candidate_history(email):
    """Load candidate history from PostgreSQL."""
    if not _db_available:
        return []
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_summary FROM candidate_history WHERE email = %s ORDER BY created_at ASC", (email,))
                rows = cur.fetchall()
                history = []
                for row in rows:
                    summary = row[0]
                    if isinstance(summary, str):
                        import json
                        summary = json.loads(summary)
                    history.append(summary)
                return history
    except Exception as e:
        log.error(f"[DB] get_candidate_history failed: {e}")
        return []


# ── Face References ──────────────────────────────────────────────────────

def save_face_reference(email, face_image_bytes, liveness_confidence=0, wearing_glasses=False):
    """Store or update a candidate's face reference image (bytes) keyed by email."""
    if not _pool:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO face_references (email, face_image, liveness_confidence, wearing_glasses, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (email) DO UPDATE SET
                        face_image = EXCLUDED.face_image,
                        liveness_confidence = EXCLUDED.liveness_confidence,
                        wearing_glasses = EXCLUDED.wearing_glasses,
                        updated_at = NOW()
                """, (email, face_image_bytes, liveness_confidence, wearing_glasses))
                conn.commit()
        log.info(f"[DB] Face reference saved for {email} ({len(face_image_bytes)} bytes, glasses={wearing_glasses})")
        return True
    except Exception as e:
        log.error(f"[DB] save_face_reference failed: {e}")
        return False


def get_face_reference(email):
    """Retrieve a candidate's face reference image bytes. Returns (bytes, confidence, wearing_glasses) or (None, 0, False)."""
    if not _pool:
        return None, 0, False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT face_image, liveness_confidence, wearing_glasses FROM face_references WHERE email = %s", (email,))
                row = cur.fetchone()
            if row:
                face_bytes = bytes(row[0]) if row[0] else None
                return face_bytes, float(row[1] or 0), bool(row[2])
            return None, 0, False
    except Exception as e:
        log.error(f"[DB] get_face_reference failed: {e}")
        return None, 0, False


# ── Expert Reviews ──────────────────────────────────────────────────────

def save_expert_review(review_id, session_id, turn_number, reviewer_name="unknown",
                       reviewer_domain="", reviewer_expertise="", ai_score=0,
                       human_score=0, verdict="", feedback="", error_flags=None):
    if not _db_available:
        return False
    try:
        import json
        score_delta = round(human_score - ai_score, 2)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO expert_reviews
                        (review_id, session_id, turn_number, reviewer_name, reviewer_domain,
                         reviewer_expertise, ai_score, human_score, score_delta, verdict,
                         feedback, error_flags)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (review_id) DO NOTHING
                """, (review_id, session_id, turn_number, reviewer_name, reviewer_domain,
                      reviewer_expertise, ai_score, human_score, score_delta, verdict,
                      feedback, json.dumps(error_flags or [])))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] save_expert_review failed: {e}")
        return False


def list_expert_reviews(session_id=None, limit=100):
    if not _db_available:
        return []
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if session_id is not None:
                    cur.execute("""
                        SELECT * FROM expert_reviews WHERE session_id = %s
                        ORDER BY reviewed_at DESC LIMIT %s
                    """, (session_id, limit))
                else:
                    cur.execute("SELECT * FROM expert_reviews ORDER BY reviewed_at DESC LIMIT %s", (limit,))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] list_expert_reviews failed: {e}")
        return []


# ── Cognition AI ────────────────────────────────────────────────────────

def save_cognition_signal(signal_type, severity, domain="", level="",
                          session_id="", turn_number=0, evidence=None):
    if not _db_available:
        return None
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cognition_signals
                        (signal_type, severity, domain, level, session_id, turn_number, evidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (signal_type, severity, domain, level, session_id, turn_number,
                      json.dumps(evidence or {})))
                conn.commit()
                return cur.fetchone()[0]
    except Exception as e:
        log.error(f"[DB] save_cognition_signal failed: {e}")
        return None


def list_cognition_signals(signal_type=None, domain=None, level=None,
                           severity=None, since=None, limit=200):
    if not _db_available:
        return []
    try:
        from psycopg.rows import dict_row
        clauses, params = [], []
        if signal_type:
            clauses.append("signal_type = %s"); params.append(signal_type)
        if domain:
            clauses.append("domain = %s"); params.append(domain)
        if level:
            clauses.append("level = %s"); params.append(level)
        if severity:
            clauses.append("severity = %s"); params.append(severity)
        if since:
            clauses.append("created_at >= %s"); params.append(since)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f"SELECT * FROM cognition_signals {where} ORDER BY created_at DESC LIMIT %s", params)
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] list_cognition_signals failed: {e}")
        return []


def cognition_signal_summary():
    if not _db_available:
        return []
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT signal_type, severity, domain, level, COUNT(*) as count,
                           MAX(created_at) as latest
                    FROM cognition_signals
                    GROUP BY signal_type, severity, domain, level
                    ORDER BY count DESC
                """)
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] cognition_signal_summary failed: {e}")
        return []


def count_recent_signals(domain, level, hours=24):
    if not _db_available:
        return 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM cognition_signals
                    WHERE domain = %s AND level = %s
                      AND created_at >= NOW() - (%s || ' hours')::interval
                """, (domain, level, str(hours)))
                return cur.fetchone()[0]
    except Exception as e:
        log.error(f"[DB] count_recent_signals failed: {e}")
        return 0


def save_cognition_diagnosis(domain, level, signal_ids, prompt_section,
                             problem, suggestion):
    if not _db_available:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cognition_diagnoses
                        (domain, level, signal_ids, prompt_section, problem, suggestion)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (domain, level, signal_ids, prompt_section, problem, suggestion))
                conn.commit()
                return cur.fetchone()[0]
    except Exception as e:
        log.error(f"[DB] save_cognition_diagnosis failed: {e}")
        return None


def list_cognition_diagnoses(status=None, domain=None, limit=100):
    if not _db_available:
        return []
    try:
        from psycopg.rows import dict_row
        clauses, params = [], []
        if status:
            clauses.append("status = %s"); params.append(status)
        if domain:
            clauses.append("domain = %s"); params.append(domain)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f"SELECT * FROM cognition_diagnoses {where} ORDER BY created_at DESC LIMIT %s", params)
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] list_cognition_diagnoses failed: {e}")
        return []


def update_diagnosis_status(diagnosis_id, status):
    if not _db_available:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                resolved = "NOW()" if status in ("applied", "dismissed") else "NULL"
                cur.execute(f"""
                    UPDATE cognition_diagnoses
                    SET status = %s, resolved_at = {resolved}
                    WHERE id = %s
                """, (status, diagnosis_id))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] update_diagnosis_status failed: {e}")
        return False


def get_cognition_state(key):
    if not _db_available:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state_value FROM cognition_state WHERE state_key = %s", (key,))
                row = cur.fetchone()
                if row:
                    val = row[0]
                    if isinstance(val, str):
                        import json
                        return json.loads(val)
                    return val
                return None
    except Exception as e:
        log.error(f"[DB] get_cognition_state failed: {e}")
        return None


def save_cognition_state(key, value):
    if not _db_available:
        return False
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cognition_state (state_key, state_value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (state_key) DO UPDATE SET
                        state_value = EXCLUDED.state_value,
                        updated_at = NOW()
                """, (key, json.dumps(value)))
                conn.commit()
                return True
    except Exception as e:
        log.error(f"[DB] save_cognition_state failed: {e}")
        return False


def list_sessions_since(since_ts, limit=200):
    """Fetch ended sessions updated after a timestamp. For cognition sweeper."""
    if not _db_available:
        return []
    try:
        import json as _json
        from datetime import datetime, timezone
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT session_id, session_data FROM active_sessions
                    WHERE session_data->>'phase' = 'ended'
                      AND updated_at >= %s
                    ORDER BY updated_at ASC LIMIT %s
                """, (since_ts, limit))
                out = []
                for sid, data in cur.fetchall():
                    if isinstance(data, str):
                        data = _json.loads(data)
                    out.append((sid, data))
                return out
    except Exception as e:
        log.error(f"[DB] list_sessions_since failed: {e}")
        return []


def get_recent_score_drift_stats(hours=168):
    """Average absolute score_delta from expert reviews in the last N hours."""
    if not _db_available:
        return {}
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT er.reviewer_domain as domain,
                           AVG(ABS(er.score_delta)) as avg_delta,
                           COUNT(*) as review_count
                    FROM expert_reviews er
                    WHERE er.reviewed_at >= NOW() - (%s || ' hours')::interval
                    GROUP BY er.reviewer_domain
                """, (str(hours),))
                return {r["domain"]: dict(r) for r in cur.fetchall()}
    except Exception as e:
        log.error(f"[DB] get_recent_score_drift_stats failed: {e}")
        return {}


# ═══════════════════════════════════════════════════════════════════
# USER INTERVIEW QUOTA (Lifetime 3-hour limit)
# ═══════════════════════════════════════════════════════════════════

def get_user_quota(email):
    """Get user's quota info. Returns dict with total_used, limit, remaining.
    Creates entry with default 180 minutes if doesn't exist."""
    if not _db_available:
        return None
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # First ensure the user exists
                cur.execute("""
                    INSERT INTO user_interview_quota (email, total_minutes_used, quota_limit_minutes)
                    VALUES (%s, 0, 180)
                    ON CONFLICT (email) DO NOTHING
                """, (email,))

                # Then fetch the current quota
                cur.execute("""
                    SELECT email, total_minutes_used, quota_limit_minutes,
                           (quota_limit_minutes - total_minutes_used) as remaining_minutes,
                           last_interview_at, created_at, updated_at
                    FROM user_interview_quota
                    WHERE email = %s
                """, (email,))
                row = cur.fetchone()
                conn.commit()
                if row:
                    return dict(row)
                return None
    except Exception as e:
        log.error(f"[DB] get_user_quota failed: {e}")
        return None


def update_user_quota(email, minutes_used):
    """Add minutes to user's total quota usage. Called when interview ends."""
    if not _db_available:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_interview_quota (email, total_minutes_used, last_interview_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (email) DO UPDATE SET
                        total_minutes_used = user_interview_quota.total_minutes_used + %s,
                        last_interview_at = NOW(),
                        updated_at = NOW()
                """, (email, minutes_used, minutes_used))
                conn.commit()
                log.info(f"[Quota] Added {minutes_used} minutes to {email}")
                return True
    except Exception as e:
        log.error(f"[DB] update_user_quota failed: {e}")
        return False


def admin_reset_user_quota(email):
    """Admin function: Reset user's quota to 0 (gives them full 3 hours again)."""
    if not _db_available:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE user_interview_quota
                    SET total_minutes_used = 0, updated_at = NOW()
                    WHERE email = %s
                """, (email,))
                conn.commit()
                log.info(f"[Quota] Admin reset quota for {email}")
                return True
    except Exception as e:
        log.error(f"[DB] admin_reset_user_quota failed: {e}")
        return False


def admin_set_user_quota_limit(email, limit_minutes):
    """Admin function: Set custom quota limit for specific user."""
    if not _db_available:
        return False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_interview_quota (email, quota_limit_minutes)
                    VALUES (%s, %s)
                    ON CONFLICT (email) DO UPDATE SET
                        quota_limit_minutes = %s,
                        updated_at = NOW()
                """, (email, limit_minutes, limit_minutes))
                conn.commit()
                log.info(f"[Quota] Admin set quota limit to {limit_minutes} min for {email}")
                return True
    except Exception as e:
        log.error(f"[DB] admin_set_user_quota_limit failed: {e}")
        return False


def get_all_user_quotas(limit=100):
    """Admin function: List all users with their quota usage."""
    if not _db_available:
        return []
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT email, total_minutes_used, quota_limit_minutes,
                           (quota_limit_minutes - total_minutes_used) as remaining_minutes,
                           last_interview_at, created_at, updated_at
                    FROM user_interview_quota
                    ORDER BY total_minutes_used DESC
                    LIMIT %s
                """, (limit,))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"[DB] get_all_user_quotas failed: {e}")
        return []
