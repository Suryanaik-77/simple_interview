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
    session_id              INTEGER NOT NULL REFERENCES sessions(id),
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
