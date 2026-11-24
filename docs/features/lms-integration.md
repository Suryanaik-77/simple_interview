Latest change: suryanaik, 2026-11-23 — end_reason/end_message on every ended interview (view columns, get-session, transcribe/stream/submit end responses); LMS face_check snapshots are now compared server-side every 60 s and end the interview after 3 failures in a row.

# LMS Integration

EduSpark LMS creates interview sessions on this app and reads results back. All interview data lives in this app's Postgres (`active_sessions`, `candidate_history`); the LMS stores none of it.

## Endpoints called by the LMS (header `X-API-Key: <LMS_API_KEY>`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/lms/launch` | Create a session. 403 `interview_limit_reached` when the student is at their limit. JSON response includes `interview_number`, `max_interviews`, `interviews_remaining`. |
| GET | `/api/lms/quota?email=` | `interview_count`, `max_interviews`, `interviews_remaining` |
| GET | `/api/lms/interview-results` | Results from the `lms_interview_results` view |

The LMS can also query `lms_interview_results` directly as the read-only `lms_reader` user (created once by `lms_db_setup.sql`). `schema.sql` drops and recreates the view on every app start, which removes privileges, so it re-grants `SELECT` to `lms_reader` right after creating the view (skipped if the role doesn't exist).

The LMS base URL must be the bare host; the launch path is `/api/lms/launch` (a doubled `/api/lms/launch/api/lms/launch` returns 404).

## Interview settings (admin-editable)

Admin panel → Configuration → **Interview Settings**. Stored in `RUNTIME_CONFIG` (`app_config` table, key `runtime`); applies immediately, no restart.

| Key | Default (env) | Range | Effect |
|---|---|---|---|
| `session_minutes` | 30 (`SESSION_MAX_DURATION_SEC/60`) | 5–180 | Interview time limit (`_session_max_sec()`), including interviews in progress |
| `max_interviews_per_user` | 4 (`MAX_INTERVIEWS_PER_USER`) | 1–100 | Global interview limit (`_default_interview_limit()`); per-student overrides win |
| `pass_percentage` | 60 (`PASS_PERCENTAGE`) | 1–100 | Scored interview passes when `overall_score × 10 ≥ pass_percentage` |

- Endpoints (`require_admin`): `GET /api/admin/interview-settings` → `{settings, limits}`; `POST /api/admin/interview-settings` with any subset of the keys (whole numbers, validated against the ranges).
- At scoring time the evaluation stores `score_percentage`, `pass_percentage` and `passed`, so a later change to the pass mark doesn't alter past results. Returned in `/api/generate-report` scores, shown on the student report card, and exposed as `score_percentage`, `pass_percentage`, `passed` columns at the end of the `lms_interview_results` view (NULL for older results).

## Interview limit

- Default limit: `max_interviews_per_user` setting (see above).
- Per-student override: `user_interview_quota.max_interviews` (NULL = default).
- What counts: a `candidate_history` row with at least 1 answered question, created after `user_interview_quota.interviews_reset_at` (rows saved before `answered` was recorded fall back to `turns`), **plus** each `interview_quota_consumptions` row (missed LMS booking) whose `occurred_at` is after the reset.
- Student emails are trimmed and lower-cased (`_norm_email` in `main.py`); older mixed-case rows are matched with `LOWER(TRIM(email))`.
- History rows are written on manual end, LLM/stop-agent end, and stale timeout (only when ≥1 answered). 0-answer sessions never count.
- Reset sets `interviews_reset_at = NOW()`; `candidate_history` is never deleted, so repeat-question avoidance and session comparison keep working.
- Checked in `/api/lms/launch` and `/api/start` via `_interview_usage()` in `main.py`; storage functions in `database.py` (`get_interview_usage`, `list_interview_usage`, `admin_reset_interviews`, `admin_set_interview_limit`).
- The old minutes quota (`total_minutes_used`, `quota_limit_minutes`) is no longer read or written; the columns remain in the table unused.

## Admin endpoints (`require_admin`: admin login cookie or `X-API-Key`)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/admin/interview-quota-list?limit=500` | — | `{default_limit, students: [{email, used, limit, remaining, custom_limit, reset_at, last_interview_at}]}` |
| POST | `/api/admin/interview-quota-reset` | `{email}` | `{ok, email, used, limit, remaining}` |
| POST | `/api/admin/interview-quota-set-limit` | `{email, limit}` (`limit: null` = back to default) | `{ok, email, used, limit, remaining}` |
| POST | `/api/admin/interview-quota-consume` | `{email, ref, reason?, occurred_at?}` | `{ok, already_recorded, email, used, limit, remaining}`; 409 with `remaining: 0` when no attempts are left |

`interview-quota-list` rows also include `missed_slots`, and list students who only have missed slots. The admin panel's Credits tab (`templates/admin.html`) uses these endpoints and shows missed slots next to Used.

### Missed bookings (`interview-quota-consume`)

Called by the LMS hourly job for each booking whose slot ended without the interview starting. Stored in `interview_quota_consumptions` (`email` lower-cased, `ref` UNIQUE, `reason`, `occurred_at`, `created_at`); no session is created and `candidate_history` is untouched.
- `ref` (LMS booking id, ≤200 chars) is required; a repeated `ref` returns 200 with `already_recorded: true` and counts once.
- `occurred_at` (ISO 8601 with timezone, the slot end time) decides whether the miss falls before or after an admin reset; default now, future values are clamped to now.
- Any valid email is accepted (a first-booking no-show has no other record with us); malformed email → 400.
- No attempts left → 409 `{ok: false, detail, remaining: 0, ...}`, nothing recorded.

## Progress and end reasons sent to clients

- Every stream-answer `done` event and every submit-answer response includes `answered_questions` (main questions only — follow-ups count with their parent) and `min_questions` (`MIN_ANSWERS_FOR_EVAL`, env, default 12). Interviews with fewer main answers are not scored.
- An unscored interview is final: its evaluation is `{status: "skipped", answered, min_questions, reason, summary}` (summary: "Not scored: N of the required 12 main questions were answered. This result is final."). In `lms_interview_results` this is `eval_status = 'skipped'`, `questions_answered`, `min_questions`, `eval_reason`, `summary`. Domain-mismatch skips have `eval_reason = 'domain_mismatch'` and no `min_questions`.
- `conversation` entries include every question asked, follow-ups too (`is_followup: true`); an entry whose `answer` is null was asked but not answered before the interview ended.
- The view adds `question_label` to each `conversation` entry: main questions `1, 2, 3…`, follow-ups `2a, 2b…` under the main question before them (computed in SQL in `schema.sql`, nothing stored). The last main number equals `questions_answered` when every main question was answered.
- `/api/end-session` accepts `reason` from the browser for anti-cheat terminations (`CLIENT_END_REASONS` in `main.py`: eye_away, tab_switch, window_resize, screen_overlay, phone_detected, multiple_persons, ai_tool_detected, camera_covered, camera_off, user_not_in_front, voice_mismatch, no_face_detected, face_mismatch). Anything else is stored as `manual`.

## Voice check (LMS `user_voice` sample)

- Runs on each `/api/transcribe` chunk of 15 s or more (`SPEAKER_MIN_AUDIO_SEC`); needs `ffmpeg` on the server (without it every check silently fails). Cosine similarity vs the LMS sample's Resemblyzer voiceprint; below `SPEAKER_VERIFY_THRESHOLD` (0.78) is a mismatch.
- First mismatched answer = warning; a 2nd consecutive mismatched answer ends the interview (`speaker_verification_failed`, browser reports `voice_mismatch`). Several chunks of the same answer count once; a verified answer resets the count.
- Reference voiceprint and mismatch count are held in `_speaker_state` (process memory) because `/api/stream-answer` writes back an older session copy; they are mirrored to `speaker_ref_embedding` / `speaker_mismatch_count` in the session for admin/LMS and for use after a restart.

## Face check during the interview (LMS page)

- The LMS interview page posts `{event_type: "face_check", metadata: {face_image: <base64 JPEG>}}` to `/api/anticheat-event` about every 30 s. The server compares one snapshot per `FACE_SNAPSHOT_INTERVAL_SEC` (60) with the registered face (AWS Rekognition, `_compare_face_image`, threshold `FACE_COMPARE_THRESHOLD`). After a failure it compares the next snapshots straight away; `FACE_SNAPSHOT_MISMATCH_LIMIT` (3) failures in a row end the interview as `face_mismatch` (or `no_face_detected`). A match resets the count.
- When a snapshot was compared the response is `{ok, face: {matched, similarity, consecutive_failures, error, should_end, end_reason, end_message}, should_end, end_reason, end_message}`; otherwise `{ok: true}`.
- State (`_face_state`: last compare time, failure count) is in process memory. Our own page (`voice_agent_ui.html`) still uses `/api/face/compare` every 60 s; both share `_compare_face_image` / `_record_face_result`.

## Why an interview ended

- `_end_interview` stores `end_reason` and `end_message` (sentence from `END_MESSAGES` in `main.py`) on the session.
- Returned by `/api/get-session` (`end_reason`, `end_message`), by `/api/transcribe` once the session has ended (`should_end`, `end_reason`, `end_message`), and on every stream-answer / submit-answer end response.
- `lms_interview_results` columns `end_reason`, `end_message` (e.g. `speaker_verification_failed` → "The interview was ended: a different speaker's voice was detected.").

## Streaming interview path (`/api/stream-answer`)

- The live UI answers through this endpoint; `/api/submit-answer` is only a fallback.
- Repeat requests ("can you repeat the question?", `_is_repeat_request`) re-ask the last question without recording an answer or advancing the turn (sent with `pause_prompt: true, repeat_question: true`).
- Repeated questions: each turn's prompt lists the questions already asked (this session + recent sessions) so the model doesn't repeat them; if one still slips through it is logged as `[DupGuard] … streamed a repeat`.

## Related fixes

- `/api/toggle-tts` and `/api/playground/tts` require admin auth.
- `/api/admin/review` reads `question_turn` / `overall_feedback` (what `admin.html` sends), falling back to `turn_number` / `feedback`.
