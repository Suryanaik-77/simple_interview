Latest change: suryanaik, 2026-11-24 — bank reference offered only on every other new topic (others come from projects / job description); "angle" references pass only the topic and key ideas, never the bank question text.

# Interview Questions (generation and repeat avoidance)

How the interviewer picks the next question, and how it avoids repeating questions within one interview and across a student's interviews. Code: `main.py` (`build_interview_prompt`, `_pick_reference_question`, `_reference_directive`, `_commit_reference`, `_previous_questions`, `_question_too_similar`).

## What the model sees each turn

| Input | Scope |
|---|---|
| Conversation (questions + answers, with expected points) | Last 6 turns |
| QUESTIONS ALREADY ASKED (steering block) | Every question of this interview + earlier interviews (see below) |
| ALREADY ASKED IN THIS INTERVIEW (live path, `/api/stream-answer`, just before generation) | Every question of this interview |
| REFERENCE FOR YOUR NEXT NEW TOPIC / REFERENCE CONCEPT | One curated bank item on every other new topic (main questions 1, 3, 5…); skipped on a forced follow-up turn. Other turns get "NO REFERENCE THIS TURN" (use projects / job description) |

Question model: `RUNTIME_CONFIG["qgen_model"]`, set in Admin → LLM config (`POST /api/admin/llm-config`). Staging and production both use `gpt-4.1-mini` (it follows the "don't repeat" list much better than `gpt-4o-mini`).

## Memory of earlier interviews

- `_answered_previous(email)`: the student's `candidate_history` rows with at least 1 answered question (matched by `LOWER(TRIM(email))`). Interviews left at the greeting are ignored — they used to push real interviews out of the old "last 3 interviews" window.
- `_previous_questions()`: real questions from all of those interviews, newest first, de-duplicated, capped at `PREV_QUESTIONS_MAX` (80). Stored per turn in `session["_prev_questions_cache"]`.

## Curated question bank as a reference

- Source: `seed_question_bank.py` (`QUESTION_BANK`, by domain and difficulty; each item `{q, topic, points}` from `question_text`, `topic`, `expected_points`). Junior levels use basic + intermediate, others intermediate + advanced, in a stable per-student order.
- Offered only when the number of main questions asked so far is even (`n_main % 2 == 0` in `build_interview_prompt`), so about half the main questions come from the bank and half from the candidate's projects / the job description.
- When offered, the server picks ONE reference the student has not had: not in `bank_refs` of earlier interviews or this one, and its core (first sentence, `_ref_core`) not similar to any question already asked (`_question_too_similar`, overlap ≥ 0.5; plurals are normalised).
- Mode is picked 50/50: `direct` (the bank question text is shown; may be asked as written or reworded) or `angle` (only the topic and up to 4 key ideas are shown — never the question text — and the model writes its own practical question: what-happens-if, debugging symptom, trade-off, real flow; no plain definition / "difference between" question).
- It is a suggestion, not a script: the model may pick another topic from the job description or the student's projects, as long as it is not already asked.
- When the bank is exhausted for a student, the least-used reference returns, always as `angle`, never twice in one interview.
- `_commit_reference()` marks the offered reference used once the turn's question is generated (both `/api/stream-answer` and `/api/submit-answer`); `save_candidate_session()` stores `bank_refs` in the history summary.

## Duplicate guard

- `/api/submit-answer` path: a generated question too similar to an asked one is regenerated once.
- `/api/stream-answer` path: the text is spoken while it streams, so a repeat can only be logged (`[DupGuard] … streamed a repeat`); prevention relies on the lists and the reference above.

## Evaluation

Follow-up questions are evaluated with their main question: they are merged into one `per_question` entry (`followup_qs`), so follow-ups never count as separate questions (`_enforce_followup_grouping`, `_count_answered`).
