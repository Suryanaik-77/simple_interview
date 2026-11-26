Latest change: suryanaik, 2026-11-26 — new Configuration → Curriculum page where trainers review and edit the key points per curriculum sub-topic (stored in `app_config`, used by the interviewer).

# Admin Panel

`templates/admin.html`, served at `/admin`; data from `require_admin` endpoints in `main.py`.

## Sessions (`sec-sessions`)

- Data: `GET /api/admin/sessions` (every session in `active_sessions`, newest first; each row carries `resume.email`).
- Stat cards (total / active / completed / high risk) count sessions, not students.
- Table: one row per student, keyed by `resume.email` (trimmed, lower-cased; falls back to candidate name when there is no email). The student row shows name + email, number of interviews, latest domain/level, "N Live" if any interview is running, the latest scored interview's score, total questions and minutes, the highest risk count, and the latest start time.
- Clicking a student row toggles the drop-down of that student's interviews (`toggleSessionGroup`, open state kept in `OPEN_SESSION_GROUPS` while the page is open). Each interview row (`sessionRowHtml`) shows the session id, phase, score, questions, duration, risk, trajectory and start date/time; clicking it opens the session detail (`openSessionDetail` → anti-cheat detail view).

## Curriculum (`sec-curriculum`)

- Script: `static/admin_curriculum.js` (loaded at the end of `admin.html`; `loadCurriculum()` runs when the section opens).
- Data: `GET /api/admin/curriculum` — every domain's active sub-topics (module groups) with `points` (trainer-saved, else the draft from `curriculum_points.py`), `draft_points`, `status` (`draft` / `approved` / `needs_change`), `note`, `edited` (points differ from the draft), `updated_by`, `updated_at`.
- Save one sub-topic: `POST /api/admin/curriculum/points` `{subtopic, points: [1-8 strings ≤200 chars], status, note}`; "Needs change" requires a note. Reset to the draft: `POST /api/admin/curriculum/reset` `{subtopic}`.
- Storage: existing `app_config` table, key `curriculum_review` — a JSON object with one entry per reviewed sub-topic, written per entry (`database.set_app_config_entry` / `delete_app_config_entry`) so two trainers saving different sub-topics never overwrite each other. No new table or column.
- UI: domain tabs with "approved x/y", progress bar, filter (all / not reviewed / needs change / approved / edited), search over sub-topic names and points; per sub-topic a points box (one per line), a note, Save / Save & approve / Needs change / Reset to draft.
- Effect on interviews: `main._key_points()` uses the saved points (cache 60 s). The status is review tracking only — draft points are used until edited.
- A sub-topic id shared by two domains (e.g. `SL / Topic1_Linux_OS_Basics`) has one shared entry.

## Other sections touched recently

- Configuration → Interview Settings (`sec-interviewsettings`): session minutes, interviews per student, pass percentage — see `lms-integration.md`.
- Credits: per-student interview limits, resets and missed LMS slots — see `lms-integration.md`.
