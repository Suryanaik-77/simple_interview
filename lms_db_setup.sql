-- ═══════════════════════════════════════════════════════════════════
-- LMS Read-Only DB Access — ONE-TIME SETUP (run as superuser)
-- Run: psql $DATABASE_URL -f lms_db_setup.sql
--
-- Creates a lms_reader user scoped ONLY to lms_interview_results.
-- The view must already exist (apply schema.sql first).
-- ═══════════════════════════════════════════════════════════════════

-- 1. Create the read-only role (change password before running)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'lms_reader') THEN
        CREATE ROLE lms_reader WITH LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
    END IF;
END
$$;

-- 2. Revoke all default privileges (belt-and-suspenders)
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM lms_reader;
REVOKE ALL ON SCHEMA public FROM lms_reader;

-- 3. Grant schema usage and SELECT on the view ONLY
GRANT USAGE ON SCHEMA public TO lms_reader;
GRANT SELECT ON lms_interview_results TO lms_reader;

-- 4. Lock down future tables — lms_reader gets nothing new by default
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM lms_reader;

-- ───────────────────────────────────────────────────────────────────
-- Connection string to give to EduSpark LMS (replace values):
--
--   postgresql://lms_reader:CHANGE_ME_STRONG_PASSWORD@<DB_HOST>:5432/<DB_NAME>
--
-- The LMS can query:
--
--   SELECT * FROM lms_interview_results WHERE email = 'student@example.com';
--   SELECT * FROM lms_interview_results WHERE session_id = '<session_id>';
--
-- Columns available:
--   session_id, email, candidate_name, domain, level, years_experience, mode
--   eval_status, overall_score, communication_score, grade, verdict, trajectory,
--   level_fit, recommendation, summary, strengths, weaknesses, topic_scores,
--   questions_answered, trust_score, ai_detection_flags, face_mismatch_count,
--   tab_switch_count, voice_mismatch_count, started_at, completed_at,
--   per_question_scores (JSON array), conversation (JSON array)
-- ───────────────────────────────────────────────────────────────────
