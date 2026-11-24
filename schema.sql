-- VLSI Interview Platform — PostgreSQL Schema

-- ═══════════════════════════════════════════
-- CANDIDATES
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS candidates (
    id              SERIAL PRIMARY KEY,
    candidate_key   TEXT UNIQUE NOT NULL,
    candidate_name  TEXT NOT NULL DEFAULT 'Candidate',
    domain          TEXT NOT NULL DEFAULT 'physical_design',
    level           TEXT NOT NULL DEFAULT 'unknown',
    years_experience REAL DEFAULT 0,
    expertise       TEXT[] DEFAULT '{}',
    tools           TEXT[] DEFAULT '{}',
    education       TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_candidates_key ON candidates(candidate_key);

-- ═══════════════════════════════════════════
-- SESSIONS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS sessions (
    id                  SERIAL PRIMARY KEY,
    session_id          TEXT UNIQUE NOT NULL,
    candidate_id        INTEGER NOT NULL REFERENCES candidates(id),
    mode                TEXT NOT NULL DEFAULT 'mock',
    domain              TEXT NOT NULL DEFAULT 'physical_design',
    level               TEXT NOT NULL DEFAULT 'trained_fresher',
    difficulty_level    INTEGER NOT NULL DEFAULT 1,
    turns_completed     INTEGER NOT NULL DEFAULT 0,
    overall_score       REAL,
    grade               TEXT,
    trajectory          TEXT DEFAULT 'unknown',
    warmup_performance  TEXT DEFAULT 'pending',
    end_reason          TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at            TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sessions_candidate ON sessions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

-- ═══════════════════════════════════════════
-- TURNS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS turns (
    id                  SERIAL PRIMARY KEY,
    session_id          INTEGER NOT NULL REFERENCES sessions(id),
    turn_number         INTEGER NOT NULL,
    phase               TEXT NOT NULL DEFAULT 'interview',
    question            TEXT NOT NULL,
    question_type       TEXT DEFAULT 'definition',
    topic               TEXT DEFAULT '',
    difficulty          TEXT DEFAULT 'basic',
    interviewer_mode    TEXT DEFAULT 'TRANSITIONING',
    answer              TEXT DEFAULT '',
    answer_duration_sec REAL DEFAULT 0,
    word_count          INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

-- ═══════════════════════════════════════════
-- EVALUATIONS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS evaluations (
    id              SERIAL PRIMARY KEY,
    turn_id         INTEGER NOT NULL REFERENCES turns(id),
    score           REAL DEFAULT 0,
    quality         TEXT DEFAULT '',
    accuracy        TEXT DEFAULT '',
    quadrant        TEXT DEFAULT '',
    score_reasoning TEXT DEFAULT '',
    expected_points JSONB DEFAULT '[]',
    missing_points  JSONB DEFAULT '[]',
    level_gap       INTEGER DEFAULT 0,
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evaluations_turn ON evaluations(turn_id);

-- ═══════════════════════════════════════════
-- BEHAVIORAL SIGNALS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS behavioral_signals (
    id                  SERIAL PRIMARY KEY,
    turn_id             INTEGER NOT NULL REFERENCES turns(id),
    filler_rate         REAL DEFAULT 0,
    pronoun_rate        REAL DEFAULT 0,
    correction_rate     REAL DEFAULT 0,
    thinking_pause_sec  REAL DEFAULT 0,
    above_level         BOOLEAN DEFAULT FALSE,
    contradiction       BOOLEAN DEFAULT FALSE,
    behavioral_flags    JSONB DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_behavioral_turn ON behavioral_signals(turn_id);

-- ═══════════════════════════════════════════
-- REPORTS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS reports (
    id                      SERIAL PRIMARY KEY,
    session_id              INTEGER NOT NULL REFERENCES sessions(id),
    technical_score         REAL DEFAULT 0,
    theory_score            REAL DEFAULT 0,
    communication_score     REAL DEFAULT 0,
    behavior_score          REAL DEFAULT 0,
    overall_score           REAL DEFAULT 0,
    topic_breakdown         JSONB DEFAULT '{}',
    strengths               JSONB DEFAULT '[]',
    weaknesses              JSONB DEFAULT '[]',
    generated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reports_session ON reports(session_id);

-- ═══════════════════════════════════════════
-- EXPERT REVIEWS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS expert_reviews (
    id                      SERIAL PRIMARY KEY,
    review_id               TEXT UNIQUE NOT NULL,
    session_id              TEXT NOT NULL DEFAULT '',
    turn_number             INTEGER NOT NULL,
    reviewer_name           TEXT DEFAULT 'unknown',
    reviewer_domain         TEXT DEFAULT '',
    reviewer_expertise      TEXT DEFAULT '',
    ai_score                REAL DEFAULT 0,
    human_score             REAL DEFAULT 0,
    score_delta             REAL DEFAULT 0,
    verdict                 TEXT DEFAULT '',
    feedback                TEXT DEFAULT '',
    error_flags             JSONB DEFAULT '[]',
    reviewed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_session ON expert_reviews(session_id);

-- Migrate expert_reviews.session_id from INTEGER FK to TEXT (matches active_sessions PK)
DO $$ BEGIN
    ALTER TABLE expert_reviews DROP CONSTRAINT IF EXISTS expert_reviews_session_id_fkey;
EXCEPTION WHEN undefined_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE expert_reviews ALTER COLUMN session_id TYPE TEXT USING session_id::TEXT;
EXCEPTION WHEN others THEN NULL;
END $$;

-- ═══════════════════════════════════════════
-- LLM CALLS
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS llm_calls (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT DEFAULT '',
    step            TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    latency_ms      REAL DEFAULT 0,
    cost_usd        REAL DEFAULT 0,
    status          TEXT DEFAULT 'success',
    error           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_llm_created ON llm_calls(created_at);

-- ═══════════════════════════════════════════
-- ACTIVE SESSIONS STATE (For process sharing)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS active_sessions (
    session_id TEXT PRIMARY KEY,
    session_data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════
-- APP CONFIG (shared runtime config: LLM/TTS/STT)
-- Durable source of truth so admin changes survive restarts and are shared
-- across all workers. Redis (when present) caches this for fast reads.
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS app_config (
    config_key TEXT PRIMARY KEY,
    config_value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════
-- CANDIDATE HISTORY (Replaces candidate_history.json)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS candidate_history (
    email TEXT NOT NULL,
    session_id TEXT PRIMARY KEY,
    session_summary JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_candidate_history_email ON candidate_history(email);

-- FACE REFERENCES (AWS Rekognition — one reference per candidate email)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS face_references (
    email TEXT PRIMARY KEY,
    face_image BYTEA NOT NULL,
    liveness_confidence REAL NOT NULL DEFAULT 0,
    wearing_glasses BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Add wearing_glasses column if table already exists (idempotent migration)
DO $$ BEGIN
    ALTER TABLE face_references ADD COLUMN wearing_glasses BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- ═══════════════════════════════════════════
-- COGNITION AI — Signal Collector & Diagnosis Engine
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cognition_signals (
    id              SERIAL PRIMARY KEY,
    signal_type     TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info',
    domain          TEXT NOT NULL DEFAULT '',
    level           TEXT NOT NULL DEFAULT '',
    session_id      TEXT DEFAULT '',
    turn_number     INTEGER DEFAULT 0,
    evidence        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cognition_signals_type ON cognition_signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_cognition_signals_created ON cognition_signals(created_at);
CREATE INDEX IF NOT EXISTS idx_cognition_signals_domain_level ON cognition_signals(domain, level);

CREATE TABLE IF NOT EXISTS cognition_diagnoses (
    id              SERIAL PRIMARY KEY,
    domain          TEXT NOT NULL DEFAULT '',
    level           TEXT NOT NULL DEFAULT '',
    signal_ids      INTEGER[] DEFAULT '{}',
    prompt_section  TEXT DEFAULT '',
    problem         TEXT DEFAULT '',
    suggestion      TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_cognition_diagnoses_status ON cognition_diagnoses(status);
CREATE INDEX IF NOT EXISTS idx_cognition_diagnoses_domain_level ON cognition_diagnoses(domain, level);

CREATE TABLE IF NOT EXISTS cognition_state (
    state_key       TEXT PRIMARY KEY,
    state_value     JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════
-- LMS READ-ONLY VIEW
-- EduSpark LMS queries this view directly using a read-only DB connection.
-- The lms_reader user (created once via lms_db_setup.sql) has SELECT on this
-- view only — nothing else in the database is visible to it.
-- ═══════════════════════════════════════════
DROP VIEW IF EXISTS lms_interview_results;
CREATE VIEW lms_interview_results AS
SELECT
    s.session_id,
    s.session_data->'resume'->>'email'              AS email,
    s.session_data->'resume'->>'candidate_name'     AS candidate_name,
    s.session_data->'resume'->>'domain'             AS domain,
    s.session_data->'resume'->>'level'              AS level,
    (s.session_data->'resume'->>'years_experience')::real AS years_experience,
    s.session_data->>'mode'                         AS mode,
    COALESCE(s.session_data->>'batch_name', '')     AS batch_name,

    -- Evaluation summary
    s.session_data->'evaluation'->>'status'         AS eval_status,
    (s.session_data->'evaluation'->>'overall_score')::real   AS overall_score,
    (s.session_data->'evaluation'->>'communication_score')::real AS communication_score,
    s.session_data->'evaluation'->>'grade'          AS grade,
    s.session_data->'evaluation'->>'verdict'        AS verdict,
    s.session_data->'evaluation'->>'trajectory'     AS trajectory,
    s.session_data->'evaluation'->>'level_fit'      AS level_fit,
    s.session_data->'evaluation'->>'recommendation' AS recommendation,
    s.session_data->'evaluation'->>'summary'        AS summary,
    s.session_data->'evaluation'->'strengths'       AS strengths,
    s.session_data->'evaluation'->'weaknesses'      AS weaknesses,
    s.session_data->'evaluation'->'topic_breakdown' AS topic_breakdown,
    (s.session_data->'evaluation'->>'answered')::int AS questions_answered,

    -- Integrity / anti-cheat
    s.session_data->'anticheat_log'->>'trust_score'          AS trust_score,
    (s.session_data->'anticheat_log'->>'ai_detection_flags')::int  AS ai_detection_flags,
    (s.session_data->'anticheat_log'->>'face_mismatch_count')::int AS face_mismatch_count,
    (s.session_data->'anticheat_log'->>'tab_switch_count')::int    AS tab_switch_count,
    (s.session_data->>'speaker_mismatch_count')::int               AS voice_mismatch_count,

    -- Timestamps
    to_timestamp((s.session_data->>'started_at')::double precision) AS started_at,
    s.updated_at                                    AS completed_at,

    -- Per-question detail with covered/missed points (JSON array)
    s.session_data->'evaluation'->'per_question'    AS per_question,
    -- Each entry gets question_label: main questions 1, 2, 3...; follow-ups 2a, 2b...
    -- (follow-ups belong to the main question before them, so the last number = questions answered)
    (SELECT jsonb_agg(y.e || jsonb_build_object('question_label', y.lbl) ORDER BY y.i)
       FROM (SELECT x.e, x.i,
                    CASE WHEN x.fu
                         THEN x.main_no || chr(96 + (ROW_NUMBER() OVER (PARTITION BY x.main_no, x.fu ORDER BY x.i))::int)
                         ELSE x.main_no::text END AS lbl
               FROM (SELECT t.e, t.i,
                            COALESCE((t.e->>'is_followup')::boolean, false) AS fu,
                            SUM(CASE WHEN COALESCE((t.e->>'is_followup')::boolean, false) THEN 0 ELSE 1 END)
                                OVER (ORDER BY t.i) AS main_no
                       FROM jsonb_array_elements(
                                CASE WHEN jsonb_typeof(s.session_data->'conversation') = 'array'
                                     THEN s.session_data->'conversation' ELSE '[]'::jsonb END
                            ) WITH ORDINALITY AS t(e, i)) x) y
    )                                               AS conversation,

    -- Pass/fail against the admin pass percentage in effect when scored (NULL for older results)
    (s.session_data->'evaluation'->>'score_percentage')::int AS score_percentage,
    (s.session_data->'evaluation'->>'pass_percentage')::int  AS pass_percentage,
    (s.session_data->'evaluation'->>'passed')::boolean       AS passed,

    -- Why a result was not scored (eval_status = 'skipped'), and the minimum main answers needed
    s.session_data->'evaluation'->>'reason'                  AS eval_reason,
    (s.session_data->'evaluation'->>'min_questions')::int    AS min_questions,

    -- Why the interview stopped (e.g. speaker_verification_failed, face_mismatch, time_limit, manual)
    -- and the sentence shown to the student (END_MESSAGES in main.py)
    s.session_data->>'end_reason'                            AS end_reason,
    s.session_data->>'end_message'                           AS end_message

FROM active_sessions s
WHERE LOWER(s.session_data->>'phase') = 'ended'
  AND s.session_data->'evaluation' IS NOT NULL
  AND s.session_data->'evaluation' != 'null'::jsonb;

-- The DROP above removes privileges on the view, so re-grant the LMS read-only
-- user on every startup (only if lms_db_setup.sql has created it).
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lms_reader') THEN
        GRANT SELECT ON lms_interview_results TO lms_reader;
    END IF;
END $$;

-- ═══════════════════════════════════════════
-- USER INTERVIEW QUOTA (Lifetime 3-hour limit)
-- Tracks total interview time used per user
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS user_interview_quota (
    id                      SERIAL PRIMARY KEY,
    email                   TEXT UNIQUE NOT NULL,
    total_minutes_used      REAL NOT NULL DEFAULT 0,
    quota_limit_minutes     REAL NOT NULL DEFAULT 180,  -- 3 hours default
    last_interview_at       TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_quota_email ON user_interview_quota(email);

-- Interview-count limit (replaces the minutes quota). NULL max_interviews = global default.
-- Only candidate_history rows created after interviews_reset_at count toward the limit,
-- so an admin reset never deletes the history used for repeat-question avoidance.
DO $$ BEGIN
    ALTER TABLE user_interview_quota ADD COLUMN max_interviews INTEGER;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE user_interview_quota ADD COLUMN interviews_reset_at TIMESTAMPTZ;
EXCEPTION WHEN duplicate_column THEN NULL;
END $$;

-- Attempts used without an interview (e.g. a missed LMS booking). Each row counts as
-- one used interview if occurred_at is after the student's interviews_reset_at.
-- ref (the LMS booking id) is unique so a repeated report counts once.
-- email is stored lower-cased and trimmed.
CREATE TABLE IF NOT EXISTS interview_quota_consumptions (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    ref         TEXT NOT NULL UNIQUE,
    reason      TEXT NOT NULL DEFAULT 'missed_slot',
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_quota_consumptions_email ON interview_quota_consumptions(email);

COMMENT ON TABLE user_interview_quota IS 'Tracks lifetime interview time quota per user (default 3 hours)';
COMMENT ON COLUMN user_interview_quota.total_minutes_used IS 'Total minutes consumed across all interviews';
COMMENT ON COLUMN user_interview_quota.quota_limit_minutes IS 'Maximum allowed minutes (admin can adjust per user)';
