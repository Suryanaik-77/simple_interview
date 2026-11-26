Latest change: suryanaik, 2026-11-26 — the question bank is removed: every interview asks only within its domain's curriculum (`curriculum.py`: Physical Design, Design Verification, Analog Layout); each new question gets a target sub-topic, never-covered sub-topics first; questions are tagged with their sub-topic and coverage is saved per interview (`subtopics_asked`). Follow-up turns stay in the last question's sub-topic; the live voice path checks for repeats before speaking. Key points per sub-topic (`curriculum_points.py`), a concept/concept/scenario/project plan with levels and styles, module weighting, project-matched project questions.

# Interview Questions (generation and repeat avoidance)

How the interviewer picks the next question, and how it avoids repeating questions within one interview and across a student's interviews. Code: `main.py` (`build_interview_prompt`, `_pick_subtopic`, `_target_directive`, `_curriculum_block`, `_commit_reference`, `_previous_questions`, `_question_too_similar`).

## What the model sees each turn

| Input | Scope |
|---|---|
| Conversation (questions + answers, with expected points) | Last 6 turns |
| QUESTIONS ALREADY ASKED (steering block) | Every question of this interview + earlier interviews (see below) |
| ALREADY ASKED IN THIS INTERVIEW (live path, `/api/stream-answer`, just before generation) | Every question of this interview |
| CONCEPTS ALREADY COVERED | Specific concepts of this interview's questions + earlier interviews (up to 60), e.g. "electromigration, clock gating" — a new topic must not reuse one, even worded differently. The live path (`/api/stream-answer`) repeats the earlier-interview concepts next to the generation |
| CURRICULUM (system prompt) | Only the student's own domain's curriculum (a DV student never sees the PD or Analog list) |
| TARGET SUB-TOPIC FOR YOUR NEXT NEW QUESTION | One curriculum sub-topic + level (BASIC / INTERMEDIATE) on every new question; skipped on a forced follow-up turn |

Question model: `RUNTIME_CONFIG["qgen_model"]`, set in Admin → LLM config (`POST /api/admin/llm-config`). Staging and production both use `gpt-4.1-mini` (it follows the "don't repeat" list much better than `gpt-4o-mini`).

## Memory of earlier interviews

- `_answered_previous(email)`: the student's `candidate_history` rows with at least 1 answered question (matched by `LOWER(TRIM(email))`). Interviews left at the greeting are ignored — they used to push real interviews out of the old "last 3 interviews" window.
- `_previous_questions()`: real questions from all of those interviews, newest first, de-duplicated, capped at `PREV_QUESTIONS_MAX` (80). Stored per turn in `session["_prev_questions_cache"]`.
- Concepts: the background tag classifier (`classify_question_tags`) also returns `concept` (1–4 words, e.g. "electromigration"), stored on the conversation entry (`database.update_session_question_tags`) and saved as `concepts_asked` in the history summary. `_previous_concepts()` collects them (newest first, `CONCEPTS_MAX` 60) into `session["_prev_concepts_cache"]`; `_concepts_block()` writes the steering text.
- Older interviews (saved before concept tagging): `_fill_missing_concepts(email)` runs once in a background thread at `/api/start-interview`, tags their questions with one `gpt-4o-mini` call per interview and stores `concepts_asked` (and `subtopics_asked`, for a domain with a curriculum) in that history row (`database.update_candidate_history_key`). Rows that already have `concepts_asked` are skipped.

## Curriculum (replaces the question bank)

- Source: `curriculum.py` — `CURRICULA` by domain, each a list of (module, sub-topics). Edit this file when a syllabus changes.
  - `physical_design`: SL, CF, VLSI Design, RTL, Fab, SSTA, PNR — 93 sub-topics
  - `design_verification`: SL, BE, Verilog, SystemVerilog, UVM — 201 sub-topics
  - `analog_layout`: SL, Basic Electronics, CL Core — 39 sub-topics
- Sub-topic ids are `"<module> / <topic>"`, e.g. `SSTA / Topic15_sdc`. `_curriculum_domain(session)` returns the session's domain if it has a curriculum; only that one curriculum is loaded for the interview.
- System prompt: `_curriculum_block(domain)` replaces the job description (the JD is used only for a domain with no curriculum). Every question (concept, project or scenario) must be on a curriculum sub-topic; most are standalone questions that don't mention the project; project questions ask about the part of the project under the sub-topic; BASIC questions are allowed (overrides the "never a simple What is X" rule for BASIC targets).
- Excluded lessons (`curriculum.EXCLUDED`, never targeted): PD "VLSI Industry"; Analog "VLSI Industry" and "Questions to Designer".
- Key points: `curriculum_points.py` (`POINTS`, 4 per sub-topic, DRAFT generated with gpt-4.1 on 2026-11-26). Trainers review and edit them in Admin → Curriculum (see `admin-panel.md`); `_key_points()` uses the saved version, else the draft. The target directive lists them and asks the model to test one not already covered.
- Per turn (not on a forced follow-up): `_pick_subtopic()` picks one TARGET SUB-TOPIC — not yet in this interview, fewest times in earlier interviews (never-asked first), the module furthest below its share (`curriculum.module_weights`: √(sub-topic count), so PNR/SSTA get more turns than RTL/Fab), per-student shuffle for ties.
- Question plan (`_target_directive()`, `_turn_type()`): new main questions repeat CONCEPT, CONCEPT, SCENARIO, PROJECT. First question BASIC; juniors get both concept slots BASIC, other levels the first BASIC and the second INTERMEDIATE with a rotating style (why / what-if / compare / check-fix). SCENARIO = a symptom to debug, not a definition. PROJECT turns target a sub-topic the candidate's project involved (`_project_subtopics()`: one gpt-4.1-mini call per interview, cached in `session["_project_subtopics"]`) and name the project; if none is left, that turn is a CONCEPT. The plan overrides the TYPE MIX.
- Phrasing: the persona's stock lead-ins ("tell me one thing", "walk me through it") at most once per interview; the directive lists the ones already used. The live path (`/api/stream-answer`) repeats the target next to the generation.
- Forced follow-up turn (the existing follow-up check fired — unchanged rule): no new target; the steering says "FOLLOW-UP TURN — stay inside the sub-topic of your last question" (`_current_subtopic()`), and the TYPE MIX / SCENARIO DUE reminders are left out that turn (they told the model a pending follow-up could wait, so it drifted to a new topic).
- Tagging: `classify_question_tags` also returns the sub-topic number from the domain's list → `entry["subtopic"]` (`database.update_session_question_tags`). Each new entry carries what the interviewer was told (`_new_question_entry()`: `target_subtopic`, or `followup_requested`), passed to the tagger as a hint. A follow-up takes its main question's sub-topic. `_commit_reference()` records the offered target in `session["subtopic_targets"]`.
- History: `save_candidate_session()` stores `subtopics_asked` (tagged sub-topics; the targets if none were tagged). Older interviews are mapped once in the background by `_fill_missing_concepts()` (one `gpt-4o-mini` call per interview; `database.update_candidate_history_key`).
- The TYPE MIX, follow-up logic, QUESTIONS ALREADY ASKED and CONCEPTS ALREADY COVERED are unchanged. `seed_question_bank.py` is no longer read by the interviewer.

## Duplicate guard

- Check: `_question_too_similar()` (stemmed concept words; question-template filler like "walk me through", "what kind of" is ignored; 2 shared words only count as a repeat at Jaccard ≥ 0.5, the overlap rule needs 3+ shared words — so "what is AOCV" vs "what are uncertainties … in timing analysis" is not a repeat, "what are guard rings" vs "what is a guard ring" is).
- Regeneration (`_dup_retry_instruction()`): one retry asking for a different question on the same TARGET SUB-TOPIC. Not on a forced follow-up turn.
- `/api/submit-answer` path: checked before the question is returned.
- `/api/stream-answer` (live voice) path: a leading short acknowledgement ("Okay.", `_is_short_ack()`) is spoken at once; the question itself is held until the full text is generated and checked, then shown and spoken (or the regenerated one, without a second acknowledgement).

## Evaluation

Follow-up questions are evaluated with their main question: they are merged into one `per_question` entry (`followup_qs`), so follow-ups never count as separate questions (`_enforce_followup_grouping`, `_count_answered`).
