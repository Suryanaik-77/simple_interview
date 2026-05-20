"""
VLSI Interview Platform — PostgreSQL Database Module
"""
import os
import time
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool = None
_db_available = False


def init_db():
    """Initialize connection pool and create tables. Called once at startup."""
    global _pool, _db_available
    if not DATABASE_URL:
        print("[DB] DATABASE_URL not set — running without database (in-memory only)")
        return

    try:
        import psycopg
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=10
        )

        # Create tables from schema.sql
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        if os.path.exists(schema_path):
            with get_conn() as conn:
                with conn.cursor() as cur:
                    with open(schema_path, "r") as f:
                        cur.execute(f.read())
                conn.commit()

        _db_available = True
        print(f"[DB] PostgreSQL connected and tables ready")
    except ImportError:
        print("[DB] psycopg2 not installed — pip install psycopg2-binary")
    except Exception as e:
        print(f"[DB] PostgreSQL connection failed: {e}")
        print("[DB] Running without database (in-memory only)")


def is_available():
    return _db_available


@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
    finally:
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
        print(f"[DB] get_or_create_candidate failed: {e}")
        return None


def get_candidate_sessions(candidate_key):
    """Returns list of past session summaries for a candidate."""
    if not _db_available:
        return []
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Get all sessions for this candidate
                cur.execute("""
                    SELECT s.id as db_id, s.session_id, s.overall_score, s.difficulty_level,
                           s.turns_completed, s.started_at, s.grade, s.mode,
                           s.early_end_reason, s.warmup_performance
                    FROM sessions s
                    JOIN candidates c ON s.candidate_id = c.id
                    WHERE c.candidate_key = %s
                    ORDER BY s.started_at ASC
                """, (candidate_key,))
                sessions = cur.fetchall()

                return [
                    {
                        "session_id": sess["session_id"],
                        "date": sess["started_at"].strftime("%Y-%m-%d %H:%M") if sess["started_at"] else "",
                        "overall_score": sess["overall_score"] or 0,
                        "difficulty_level": sess["difficulty_level"] or 1,
                        "turns_completed": sess["turns_completed"] or 0,
                        "warmup_performance": sess["warmup_performance"] or "pending",
                    }
                    for sess in sessions
                ]
    except Exception as e:
        print(f"[DB] get_candidate_sessions failed: {e}")
        return []


def get_candidate_session_count(candidate_key):
    """Fast check: how many completed sessions does this candidate have?"""
    if not _db_available:
        return 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM sessions s
                    JOIN candidates c ON s.candidate_id = c.id
                    WHERE c.candidate_key = %s
                """, (candidate_key,))
                return cur.fetchone()[0]
    except Exception as e:
        print(f"[DB] get_candidate_session_count failed: {e}")
        return 0


def save_session(session_id, candidate_id, mode, domain, level, difficulty_level,
                 turns_completed, overall_score, grade, trajectory, warmup_performance,
                 end_reason, started_at):
    """Save complete session data to DB. Idempotent."""
    if not _db_available or not candidate_id:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sessions
                        (session_id, candidate_id, mode, domain, level, difficulty_level,
                         turns_completed, overall_score, grade, trajectory, warmup_performance,
                         end_reason, started_at, ended_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        difficulty_level = EXCLUDED.difficulty_level,
                        turns_completed = EXCLUDED.turns_completed,
                        overall_score = EXCLUDED.overall_score,
                        grade = EXCLUDED.grade,
                        trajectory = EXCLUDED.trajectory,
                        warmup_performance = EXCLUDED.warmup_performance,
                        end_reason = EXCLUDED.end_reason,
                        ended_at = NOW()
                    RETURNING id
                """, (session_id, candidate_id, mode, domain, level, difficulty_level,
                      turns_completed, overall_score, grade, trajectory, warmup_performance,
                      end_reason, started_at))
                db_id = cur.fetchone()[0]
                conn.commit()
                print(f"[DB] Session {session_id[:8]} saved: score={overall_score}, turns={turns_completed}")
                return db_id
    except Exception as e:
        print(f"[DB] save_session failed: {e}")
        return None


def save_turn(db_session_id, turn_number, phase, question, question_type, topic,
              difficulty, interviewer_mode, answer, answer_duration_sec, word_count):
    """Save a single turn. Returns turn DB id."""
    if not _db_available or not db_session_id:
        return None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO turns (session_id, turn_number, phase, question, question_type,
                                       topic, difficulty, interviewer_mode, answer,
                                       answer_duration_sec, word_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (db_session_id, turn_number, phase, question, question_type,
                      topic, difficulty, interviewer_mode, answer or "",
                      answer_duration_sec, word_count))
                conn.commit()
                return cur.fetchone()[0]
    except Exception as e:
        print(f"[DB] save_turn failed: {e}")
        return None


def save_evaluation(turn_id, score, quality, accuracy, quadrant, score_reasoning,
                    expected_points, missing_points, level_gap, notes):
    """Save evaluation for a turn."""
    if not _db_available or not turn_id:
        return
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO evaluations (turn_id, score, quality, accuracy, quadrant,
                                             score_reasoning, expected_points, missing_points,
                                             level_gap, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (turn_id, score, quality, accuracy, quadrant, score_reasoning,
                      json.dumps(expected_points or []), json.dumps(missing_points or []),
                      level_gap, notes))
                conn.commit()
    except Exception as e:
        print(f"[DB] save_evaluation failed: {e}")


def save_behavioral(turn_id, filler_rate, pronoun_rate, correction_rate,
                    thinking_pause_sec, above_level, contradiction, behavioral_flags):
    """Save behavioral signals for a turn."""
    if not _db_available or not turn_id:
        return
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO behavioral_signals (turn_id, filler_rate, pronoun_rate,
                                                    correction_rate, thinking_pause_sec,
                                                    above_level, contradiction, behavioral_flags)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (turn_id, filler_rate, pronoun_rate, correction_rate,
                      thinking_pause_sec, above_level, contradiction,
                      json.dumps(behavioral_flags or [])))
                conn.commit()
    except Exception as e:
        print(f"[DB] save_behavioral failed: {e}")


def save_llm_call(session_id, step, model, input_tokens, output_tokens,
                  latency_ms, cost_usd, status, error=""):
    """Save LLM call to DB for cost tracking."""
    if not _db_available:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO llm_calls (session_id, step, model, input_tokens, output_tokens,
                                           latency_ms, cost_usd, status, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (session_id, step, model, input_tokens or 0, output_tokens or 0,
                      latency_ms, cost_usd or 0, status, error or ""))
                conn.commit()
    except Exception as e:
        print(f"[DB] save_llm_call failed: {e}")


def save_report(db_session_id, technical_score, theory_score, communication_score,
                behavior_score, overall_score, topic_breakdown, strengths, weaknesses):
    """Save final interview report."""
    if not _db_available or not db_session_id:
        return
    try:
        import json
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO reports (session_id, technical_score, theory_score,
                                         communication_score, behavior_score, overall_score,
                                         topic_breakdown, strengths, weaknesses)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (db_session_id, technical_score, theory_score, communication_score,
                      behavior_score, overall_score, json.dumps(topic_breakdown or {}),
                      json.dumps(strengths or []), json.dumps(weaknesses or [])))
                conn.commit()
    except Exception as e:
        print(f"[DB] save_report failed: {e}")
