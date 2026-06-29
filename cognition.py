"""
Cognition AI — Self-improving signal collector & diagnosis engine.

Phase 1: Signal collectors detect prompt quality issues from session data.
Phase 2: Diagnosis engine uses LLM to suggest prompt fixes when signals accumulate.
"""
import os
import time
import json
import threading
import statistics
from datetime import datetime, timezone

import database

COGNITION_SWEEP_INTERVAL_SEC = int(os.getenv("COGNITION_SWEEP_INTERVAL_SEC", "600"))
DIAGNOSIS_SIGNAL_THRESHOLD = 3
DIAGNOSIS_COOLDOWN_HOURS = 24

_WATERMARK_KEY = "cognition_sweep_watermark"


# ── Signal Collectors (Phase 1) ────────────────────────────────────────

def collect_eval_parse_fail(sessions):
    """Sessions where evaluation had parse errors or is missing/malformed."""
    signals = []
    for sid, data in sessions:
        ev = data.get("evaluation")
        if not ev or not isinstance(ev, dict):
            continue
        if ev.get("parse_error") or ev.get("error"):
            domain = data.get("resume", {}).get("domain", "")
            level = data.get("resume", {}).get("level", "")
            signals.append({
                "signal_type": "eval_parse_fail",
                "severity": "warning",
                "domain": domain,
                "level": level,
                "session_id": sid,
                "evidence": {
                    "error": ev.get("parse_error") or ev.get("error", ""),
                    "model": data.get("obs_log", [{}])[-1].get("model", ""),
                },
            })
    return signals


def collect_difficulty_ramp(sessions):
    """Sessions where per-question scores show a falling trajectory."""
    signals = []
    for sid, data in sessions:
        ev = data.get("evaluation")
        if not ev or not isinstance(ev, dict):
            continue
        per_q = ev.get("per_question_scores") or ev.get("per_question", [])
        if not isinstance(per_q, list) or len(per_q) < 4:
            continue
        scores = []
        for q in per_q:
            if isinstance(q, dict):
                s = q.get("score") or q.get("rating")
                if s is not None:
                    try:
                        scores.append(float(s))
                    except (ValueError, TypeError):
                        pass
        if len(scores) < 4:
            continue
        mid = len(scores) // 2
        first_half = statistics.mean(scores[:mid])
        second_half = statistics.mean(scores[mid:])
        if first_half > 0 and (second_half - first_half) / first_half < -0.20:
            domain = data.get("resume", {}).get("domain", "")
            level = data.get("resume", {}).get("level", "")
            signals.append({
                "signal_type": "difficulty_ramp",
                "severity": "warning",
                "domain": domain,
                "level": level,
                "session_id": sid,
                "evidence": {
                    "first_half_avg": round(first_half, 2),
                    "second_half_avg": round(second_half, 2),
                    "drop_pct": round((second_half - first_half) / first_half * 100, 1),
                    "score_count": len(scores),
                },
            })
    return signals


def collect_topic_gap(sessions):
    """Resume skills that were never asked about in the interview."""
    signals = []
    for sid, data in sessions:
        resume = data.get("resume", {})
        skills = resume.get("expertise") or resume.get("skills") or []
        if not skills:
            continue
        conv = data.get("conversation", [])
        asked_text = " ".join(
            (e.get("question", "") + " " + e.get("topic", "")).lower()
            for e in conv if isinstance(e, dict)
        )
        missed = [s for s in skills if s.lower() not in asked_text]
        if missed and len(missed) >= 2:
            domain = resume.get("domain", "")
            level = resume.get("level", "")
            signals.append({
                "signal_type": "topic_gap",
                "severity": "info",
                "domain": domain,
                "level": level,
                "session_id": sid,
                "evidence": {
                    "resume_skills": skills,
                    "missed_skills": missed,
                    "turns": len(conv),
                },
            })
    return signals


def collect_cost_anomaly(sessions):
    """Sessions with token usage > 2 std devs from the batch mean."""
    if len(sessions) < 5:
        return []
    totals = []
    for sid, data in sessions:
        obs = data.get("obs_log", [])
        total = sum(e.get("input_tokens", 0) + e.get("output_tokens", 0)
                    for e in obs if isinstance(e, dict))
        totals.append((sid, data, total))
    token_vals = [t for _, _, t in totals if t > 0]
    if len(token_vals) < 5:
        return []
    mean = statistics.mean(token_vals)
    stdev = statistics.stdev(token_vals)
    if stdev == 0:
        return []
    signals = []
    for sid, data, total in totals:
        if total > 0 and (total - mean) > 2 * stdev:
            domain = data.get("resume", {}).get("domain", "")
            level = data.get("resume", {}).get("level", "")
            signals.append({
                "signal_type": "cost_anomaly",
                "severity": "warning",
                "domain": domain,
                "level": level,
                "session_id": sid,
                "evidence": {
                    "total_tokens": total,
                    "batch_mean": round(mean),
                    "batch_stdev": round(stdev),
                    "z_score": round((total - mean) / stdev, 2),
                },
            })
    return signals


def collect_score_drift():
    """Expert vs AI score disagreement from expert_reviews table."""
    stats = database.get_recent_score_drift_stats(hours=168)
    signals = []
    for domain, info in stats.items():
        if info.get("avg_delta", 0) > 1.5 and info.get("review_count", 0) >= 3:
            signals.append({
                "signal_type": "score_drift",
                "severity": "critical" if info["avg_delta"] > 2.5 else "warning",
                "domain": domain,
                "level": "",
                "session_id": "",
                "evidence": {
                    "avg_score_delta": round(info["avg_delta"], 2),
                    "review_count": info["review_count"],
                },
            })
    return signals


def collect_followup_quality(sessions):
    """Follow-up questions scoring consistently lower than main questions."""
    signals = []
    for sid, data in sessions:
        ev = data.get("evaluation")
        if not ev or not isinstance(ev, dict):
            continue
        per_q = ev.get("per_question_scores") or ev.get("per_question", [])
        if not isinstance(per_q, list) or len(per_q) < 3:
            continue
        main_scores, followup_scores = [], []
        conv = data.get("conversation", [])
        for i, q in enumerate(per_q):
            if not isinstance(q, dict):
                continue
            score = q.get("score") or q.get("rating")
            if score is None:
                continue
            try:
                score = float(score)
            except (ValueError, TypeError):
                continue
            is_followup = False
            if i < len(conv) and isinstance(conv[i], dict):
                is_followup = conv[i].get("is_followup", False) or conv[i].get("followup_of") is not None
            if is_followup:
                followup_scores.append(score)
            else:
                main_scores.append(score)
        if len(followup_scores) >= 3 and len(main_scores) >= 3:
            main_avg = statistics.mean(main_scores)
            fu_avg = statistics.mean(followup_scores)
            if main_avg > 0 and (fu_avg - main_avg) / main_avg < -0.25:
                domain = data.get("resume", {}).get("domain", "")
                level = data.get("resume", {}).get("level", "")
                signals.append({
                    "signal_type": "followup_quality",
                    "severity": "warning",
                    "domain": domain,
                    "level": level,
                    "session_id": sid,
                    "evidence": {
                        "main_avg": round(main_avg, 2),
                        "followup_avg": round(fu_avg, 2),
                        "gap_pct": round((fu_avg - main_avg) / main_avg * 100, 1),
                    },
                })
    return signals


ALL_COLLECTORS = [
    collect_eval_parse_fail,
    collect_difficulty_ramp,
    collect_topic_gap,
    collect_cost_anomaly,
    collect_followup_quality,
]

REVIEW_COLLECTORS = [
    collect_score_drift,
]


# ── Sweeper ─────────────────────────────────────────────────────────────

def run_sweep():
    """Run all signal collectors on sessions since last watermark.
    Returns count of new signals saved."""
    if not database.is_available():
        return 0

    watermark = database.get_cognition_state(_WATERMARK_KEY)
    since_ts = watermark.get("since", "2000-01-01T00:00:00Z") if watermark else "2000-01-01T00:00:00Z"

    sessions = database.list_sessions_since(since_ts, limit=200)
    now_ts = datetime.now(timezone.utc).isoformat()

    all_signals = []
    for collector in ALL_COLLECTORS:
        try:
            all_signals.extend(collector(sessions))
        except Exception as e:
            print(f"[Cognition] collector {collector.__name__} failed: {e}")

    for collector in REVIEW_COLLECTORS:
        try:
            all_signals.extend(collector())
        except Exception as e:
            print(f"[Cognition] collector {collector.__name__} failed: {e}")

    saved = 0
    for sig in all_signals:
        sig_id = database.save_cognition_signal(
            signal_type=sig["signal_type"],
            severity=sig["severity"],
            domain=sig.get("domain", ""),
            level=sig.get("level", ""),
            session_id=sig.get("session_id", ""),
            turn_number=sig.get("turn_number", 0),
            evidence=sig.get("evidence"),
        )
        if sig_id:
            saved += 1

    database.save_cognition_state(_WATERMARK_KEY, {"since": now_ts})

    if saved:
        print(f"[Cognition] Sweep complete: {saved} new signal(s) from {len(sessions)} session(s)")

    _maybe_run_diagnosis(all_signals)
    return saved


# ── Diagnosis Engine (Phase 2) ──────────────────────────────────────────

_DIAGNOSIS_LOCK = threading.Lock()

def _maybe_run_diagnosis(new_signals):
    """Check if any domain+level has enough signals to warrant a diagnosis."""
    buckets = {}
    for sig in new_signals:
        key = (sig.get("domain", ""), sig.get("level", ""))
        buckets.setdefault(key, []).append(sig)

    for (domain, level), sigs in buckets.items():
        if len(sigs) < DIAGNOSIS_SIGNAL_THRESHOLD:
            continue
        total = database.count_recent_signals(domain, level, hours=DIAGNOSIS_COOLDOWN_HOURS)
        if total < DIAGNOSIS_SIGNAL_THRESHOLD:
            continue
        _run_diagnosis(domain, level, sigs)


def _run_diagnosis(domain, level, signals):
    """LLM-powered diagnosis: read prompt + signal evidence, suggest fixes."""
    if not _DIAGNOSIS_LOCK.acquire(blocking=False):
        return
    try:
        from main import call_llm, get_interview_prompt, RUNTIME_CONFIG  # noqa: E402

        recent_diagnoses = database.list_cognition_diagnoses(domain=domain, limit=5)
        for d in recent_diagnoses:
            created = d.get("created_at")
            if created:
                if isinstance(created, str):
                    created = datetime.fromisoformat(created)
                age_hours = (datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                if age_hours < DIAGNOSIS_COOLDOWN_HOURS:
                    print(f"[Cognition] Diagnosis cooldown active for {domain}/{level}")
                    return

        try:
            prompt_text = get_interview_prompt(level, domain)
        except Exception:
            prompt_text = "(prompt file not found)"

        signal_summary = json.dumps([{
            "type": s["signal_type"],
            "severity": s["severity"],
            "evidence": s.get("evidence", {}),
        } for s in signals[:10]], indent=2)

        system_msg = (
            "You are a prompt engineering expert analyzing an AI interview system. "
            "You will receive the current interview prompt and quality signals detected from real sessions. "
            "Your job is to diagnose the root cause and suggest a specific, actionable fix to the prompt.\n\n"
            "Respond with ONLY a valid JSON object:\n"
            '{"prompt_section": "the part of the prompt causing issues (quote it)", '
            '"problem": "1-2 sentence diagnosis of what is wrong", '
            '"suggestion": "the specific edit to the prompt text"}'
        )
        user_msg = (
            f"Domain: {domain}\nLevel: {level}\n\n"
            f"=== CURRENT PROMPT (first 3000 chars) ===\n{prompt_text[:3000]}\n\n"
            f"=== QUALITY SIGNALS ===\n{signal_summary}"
        )

        try:
            cognition_model = RUNTIME_CONFIG.get("cognition_model", "")
            raw, usage = call_llm(
                [{"role": "system", "content": system_msg},
                 {"role": "user", "content": user_msg}],
                model_id=cognition_model,
                temperature=0.2, max_tokens=500,
            )
            result = None
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                import re
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if match:
                    result = json.loads(match.group())

            if result and isinstance(result, dict):
                signal_ids = [s.get("id") for s in signals if s.get("id")]
                database.save_cognition_diagnosis(
                    domain=domain,
                    level=level,
                    signal_ids=signal_ids,
                    prompt_section=result.get("prompt_section", ""),
                    problem=result.get("problem", ""),
                    suggestion=result.get("suggestion", ""),
                )
                print(f"[Cognition] Diagnosis saved for {domain}/{level}: {result.get('problem', '')[:80]}")
            else:
                print(f"[Cognition] Diagnosis LLM returned unparseable output")
        except Exception as e:
            print(f"[Cognition] Diagnosis LLM call failed: {e}")
    finally:
        _DIAGNOSIS_LOCK.release()


# ── Background Thread ───────────────────────────────────────────────────

def _cognition_sweeper_loop():
    """Background loop matching eval-sweeper pattern."""
    while True:
        try:
            time.sleep(COGNITION_SWEEP_INTERVAL_SEC)
            if not database.is_available():
                continue
            run_sweep()
        except Exception as e:
            print(f"[Cognition] sweeper loop error: {e}")


def start_sweeper():
    """Start the background cognition sweeper thread."""
    threading.Thread(
        target=_cognition_sweeper_loop,
        daemon=True,
        name="cognition-sweeper",
    ).start()
    print(f"[Cognition] Sweeper started (interval={COGNITION_SWEEP_INTERVAL_SEC}s)")
