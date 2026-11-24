Latest change: suryanaik, 2026-11-24 — Sessions page groups interviews per student (one row per student, click to drop down their interviews).

# Admin Panel

`templates/admin.html`, served at `/admin`; data from `require_admin` endpoints in `main.py`.

## Sessions (`sec-sessions`)

- Data: `GET /api/admin/sessions` (every session in `active_sessions`, newest first; each row carries `resume.email`).
- Stat cards (total / active / completed / high risk) count sessions, not students.
- Table: one row per student, keyed by `resume.email` (trimmed, lower-cased; falls back to candidate name when there is no email). The student row shows name + email, number of interviews, latest domain/level, "N Live" if any interview is running, the latest scored interview's score, total questions and minutes, the highest risk count, and the latest start time.
- Clicking a student row toggles the drop-down of that student's interviews (`toggleSessionGroup`, open state kept in `OPEN_SESSION_GROUPS` while the page is open). Each interview row (`sessionRowHtml`) shows the session id, phase, score, questions, duration, risk, trajectory and start date/time; clicking it opens the session detail (`openSessionDetail` → anti-cheat detail view).

## Other sections touched recently

- Configuration → Interview Settings (`sec-interviewsettings`): session minutes, interviews per student, pass percentage — see `lms-integration.md`.
- Credits: per-student interview limits, resets and missed LMS slots — see `lms-integration.md`.
