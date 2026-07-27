-- Corrected lms_interview_results view.
-- Fixes vs. the original:
--   1. filter was phase='done' (never written) -> phase='ended' (the real completion phase)
--   2. topic_scores  -> evaluation->'topic_breakdown' (actual key)
--   3. per_question_scores -> evaluation->'per_question' (actual key)
--   4. integrity counts were read as object keys off anticheat_log, but that is a
--      JSON ARRAY of events -> now aggregated from the array / per-turn ai_detection
--   5. voice_mismatch_count -> top-level speaker_mismatch_count (COALESCE 0)
--   6. trust_score derived (categorical text) from the integrity signals
--   7. grade / trajectory kept as columns (schema-stable) but NULL: the evaluator
--      writes no such keys.
--
-- Column order/names are preserved so existing SELECT * consumers are unaffected.

CREATE OR REPLACE VIEW lms_interview_results AS
SELECT
    s.session_id,
    (s.session_data -> 'resume' ->> 'email')                          AS email,
    (s.session_data -> 'resume' ->> 'candidate_name')                 AS candidate_name,
    (s.session_data -> 'resume' ->> 'domain')                         AS domain,
    (s.session_data -> 'resume' ->> 'level')                          AS level,
    NULLIF(s.session_data -> 'resume' ->> 'years_experience', '')::real AS years_experience,
    (s.session_data ->> 'mode')                                       AS mode,
    (s.session_data -> 'evaluation' ->> 'status')                     AS eval_status,
    NULLIF(s.session_data -> 'evaluation' ->> 'overall_score', '')::real       AS overall_score,
    NULLIF(s.session_data -> 'evaluation' ->> 'communication_score', '')::real AS communication_score,
    NULL::text                                                        AS grade,        -- evaluator writes no 'grade'
    (s.session_data -> 'evaluation' ->> 'verdict')                    AS verdict,
    NULL::text                                                        AS trajectory,   -- evaluator writes no 'trajectory'
    (s.session_data -> 'evaluation' ->> 'level_fit')                  AS level_fit,
    (s.session_data -> 'evaluation' ->> 'recommendation')             AS recommendation,
    (s.session_data -> 'evaluation' ->> 'summary')                    AS summary,
    (s.session_data -> 'evaluation' -> 'strengths')                   AS strengths,
    (s.session_data -> 'evaluation' -> 'weaknesses')                  AS weaknesses,
    (s.session_data -> 'evaluation' -> 'topic_breakdown')             AS topic_scores,      -- corrected key
    NULLIF(s.session_data -> 'evaluation' ->> 'answered', '')::integer AS questions_answered,
    -- trust_score: any cheating termination => low; else banded by total integrity flags.
    CASE
        WHEN ac.terminations > 0 THEN 'low'
        WHEN (ai.ai_detection_flags + ac.face_mismatch_count + ac.tab_switch_count
              + COALESCE(NULLIF(s.session_data ->> 'speaker_mismatch_count', '')::integer, 0)) = 0 THEN 'high'
        WHEN (ai.ai_detection_flags + ac.face_mismatch_count + ac.tab_switch_count
              + COALESCE(NULLIF(s.session_data ->> 'speaker_mismatch_count', '')::integer, 0)) <= 2 THEN 'medium'
        ELSE 'low'
    END                                                               AS trust_score,
    ai.ai_detection_flags::integer                                    AS ai_detection_flags,
    ac.face_mismatch_count::integer                                   AS face_mismatch_count,
    ac.tab_switch_count::integer                                      AS tab_switch_count,
    COALESCE(NULLIF(s.session_data ->> 'speaker_mismatch_count', '')::integer, 0) AS voice_mismatch_count,
    to_timestamp(NULLIF(s.session_data ->> 'started_at', '')::double precision)   AS started_at,
    s.updated_at                                                      AS completed_at,
    (s.session_data -> 'evaluation' -> 'per_question')                AS per_question_scores,  -- corrected key
    (s.session_data -> 'conversation')                                AS conversation
FROM active_sessions s
LEFT JOIN LATERAL (
    -- integrity signals aggregated from the anticheat_log event array
    SELECT
        count(*) FILTER (WHERE e ->> 'event_type' = 'face_mismatch')               AS face_mismatch_count,
        count(*) FILTER (WHERE e ->> 'event_type' IN ('tab_switch', 'window_blur')) AS tab_switch_count,
        count(*) FILTER (WHERE e ->> 'event_type' LIKE '%\_termination')            AS terminations
    FROM jsonb_array_elements(COALESCE(s.session_data -> 'anticheat_log', '[]'::jsonb)) e
) ac ON true
LEFT JOIN LATERAL (
    -- AI-answer flags counted from per-turn ai_detection results
    SELECT count(*) FILTER (WHERE (c -> 'ai_detection' ->> 'is_ai') = 'true') AS ai_detection_flags
    FROM jsonb_array_elements(COALESCE(s.session_data -> 'conversation', '[]'::jsonb)) c
) ai ON true
WHERE (s.session_data ->> 'phase') = 'ended'
  AND (s.session_data -> 'evaluation') IS NOT NULL
  AND (s.session_data -> 'evaluation') <> 'null'::jsonb;
