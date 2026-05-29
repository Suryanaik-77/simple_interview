#!/usr/bin/env python3
"""
loadtest.py — stepped-concurrency load test for the Simple Interview API.

Pure standard library (urllib + threading), so it runs on old Pythons (3.6+)
with zero installs. It hits a remote staging URL, ramps concurrency through a
series of steps, and prints latency percentiles + throughput + error rate per
step so you can see how the service behaves as load grows.

SCENARIOS

  health   GET /health
           Pure server-capacity probe. FREE, no side effects.

  session  POST /api/create-session -> GET /api/get-session -> POST /api/end-session
           FREE (sends a pre-parsed JSON resume, so the server skips the LLM).
           Cross-worker correctness probe: create lands on one worker, the GET
           must find the session on (maybe) another. If workers don't share
           state via Postgres, the GET 404s -> reported in the "xwork404" column.

  turn     POST /api/create-session -> POST /api/submit-answer -> end
           *** COSTS REAL MONEY *** — submit-answer is the NON-streaming endpoint:
           it blocks for the FULL LLM response + FULL TTS audio, then returns.
           So this reports full-turn-completion latency (the fallback path), plus
           the server-reported llm_ms / tts_ms. Hard-capped by --max-turns/step.

  stream   POST /api/create-session -> POST /api/stream-answer (SSE) -> end
           *** COSTS REAL MONEY *** — this is the endpoint the UI actually uses.
           It consumes the SSE stream and timestamps:
             TTFT  = time to first 'token' event   (interviewer starts thinking)
             TTFA  = time to first 'audio' event    (interviewer starts SPEAKING)
             total = time to the 'done' event        (full turn)
           The p50/p95/p99 columns are TTFA — the latency a candidate actually
           perceives. Hard-capped by --max-turns/step.

  Note (turn & stream): both measure a FRESH first turn — smallest prompt and no
  background AI-detection call. Real mid-interview turns carry more context and
  also fire a background AI-detect LLM call, so production is somewhat slower and
  ~2x the LLM call volume. Errors here (429 / 5xx) usually mean PROVIDER RATE
  LIMITS, not app bugs.

USAGE
  python3 loadtest.py --url https://staging.example.com --scenario health
  python3 loadtest.py --url https://staging.example.com --scenario session
  python3 loadtest.py --url https://staging.example.com --scenario stream \
      --steps 1,10,25,50,100 --duration 10 --max-turns 60
  python3 loadtest.py --url https://staging.example.com --insecure ...   # self-signed cert
"""

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

# A realistic ~30-word answer so the turn behaves like a real one.
CANNED_ANSWER = (
    "In physical design, clock tree synthesis builds a buffered tree from the clock "
    "source to all flops to balance skew, and after CTS I usually check insertion "
    "delay, skew, and any timing pushouts before routing."
)


def do_request(method, url, body=None, timeout=30, ctx=None):
    """Issue one HTTP request. Returns (status, latency_ms, body_bytes, error).
    status is the HTTP code (incl. 4xx/5xx); error is set only on transport failures."""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        payload = resp.read()
        return resp.getcode(), (time.monotonic() - t0) * 1000.0, payload, None
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, (time.monotonic() - t0) * 1000.0, payload, None
    except Exception as e:
        return None, (time.monotonic() - t0) * 1000.0, b"", str(e)


def _resume_payload(domain):
    return json.dumps({
        "candidate_name": "Load Test",
        "domain": domain,
        "level": "trained_fresher",
        "years_experience": 1,
        "email": "",            # empty -> server skips candidate_history writes
        "skills": [], "tools": [], "key_projects": [],
    })


def _create_session(base, domain, timeout, ctx):
    """Returns (session_id or None, status, err). Does NOT call the LLM."""
    body = {"resume_text": _resume_payload(domain), "mode": "mock", "domain": domain}
    status, _ms, payload, err = do_request("POST", base + "/api/create-session", body, timeout, ctx)
    if err or status != 200:
        return None, status, (err or ("create %s" % status))
    try:
        return json.loads(payload.decode("utf-8")).get("session_id"), status, None
    except Exception as e:
        return None, status, ("bad create json: %s" % e)


def _end(base, sid, timeout, ctx):
    do_request("POST", base + "/api/end-session", {"session_id": sid}, timeout, ctx)


def op_health(base, domain, timeout, ctx):
    status, ms, _body, err = do_request("GET", base + "/health", None, timeout, ctx)
    return {"ok": status == 200, "ms": ms, "xwork404": False, "err": err, "status": status}


def op_session(base, domain, timeout, ctx):
    """Full create -> get -> end lifecycle. FREE (no LLM)."""
    t0 = time.monotonic()
    sid, status, err = _create_session(base, domain, timeout, ctx)
    if sid is None:
        return {"ok": False, "ms": (time.monotonic() - t0) * 1000.0,
                "xwork404": False, "err": err, "status": status}

    gstatus, _gms, _gp, gerr = do_request(
        "GET", base + "/api/get-session?session_id=" + str(sid), None, timeout, ctx)
    xwork404 = (gstatus == 404)
    get_ok = (gstatus == 200)
    _end(base, sid, timeout, ctx)

    total = (time.monotonic() - t0) * 1000.0
    if gerr or not get_ok:
        return {"ok": False, "ms": total, "xwork404": xwork404,
                "err": gerr or ("get %s" % gstatus), "status": gstatus}
    return {"ok": True, "ms": total, "xwork404": False, "err": None, "status": 200}


def op_turn(base, domain, timeout, ctx):
    """create -> submit-answer (REAL LLM + FULL TTS) -> end. Reports full-turn latency."""
    sid, status, err = _create_session(base, domain, timeout, ctx)
    if sid is None:
        return {"ok": False, "ms": 0.0, "xwork404": False, "err": err, "status": status}

    sstatus, sms, spayload, serr = do_request(
        "POST", base + "/api/submit-answer",
        {"session_id": sid, "answer": CANNED_ANSWER}, timeout, ctx)
    ok = (sstatus == 200 and not serr)
    llm_ms = tts_ms = None
    if ok:
        try:
            timing = json.loads(spayload.decode("utf-8")).get("timing", {})
            llm_ms = timing.get("llm_ms")
            tts_ms = timing.get("tts_ms")
        except Exception:
            pass
    _end(base, sid, timeout, ctx)

    res = {"ok": ok, "ms": sms, "xwork404": False,
           "err": serr or (None if ok else ("submit %s" % sstatus)), "status": sstatus}
    if llm_ms is not None:
        res["llm_ms"] = llm_ms
    if tts_ms is not None:
        res["tts_ms"] = tts_ms
    return res


def op_stream(base, domain, timeout, ctx):
    """create -> stream-answer (SSE, REAL LLM + per-sentence TTS) -> end.
    Timestamps the SSE events. Reported latency (ms) is TTFA — time to first audio."""
    sid, status, err = _create_session(base, domain, timeout, ctx)
    if sid is None:
        return {"ok": False, "ms": 0.0, "xwork404": False, "err": err, "status": status}

    data = json.dumps({"session_id": sid, "answer": CANNED_ANSWER}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/stream-answer", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})

    ttft = ttfa = total = None
    ok = False
    errmsg = None
    t0 = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        for raw in resp:                       # SSE lines arrive as the server flushes them
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except Exception:
                continue
            now = (time.monotonic() - t0) * 1000.0
            etype = evt.get("type")
            if etype == "token" and ttft is None:
                ttft = now
            elif etype == "audio" and ttfa is None:
                ttfa = now
            elif etype == "done":
                total = now
                ok = True
                break
        resp.close()
    except urllib.error.HTTPError as e:
        errmsg = "stream %s" % e.code
    except Exception as e:
        errmsg = str(e)
    _end(base, sid, timeout, ctx)

    # Perceived latency = time to first audio. Fall back to total if TTS is off (no audio events).
    primary = ttfa if ttfa is not None else (total if total is not None else 0.0)
    res = {"ok": ok, "ms": primary, "xwork404": False,
           "err": errmsg or (None if ok else "no done event"),
           "status": 200 if ok else None}
    if ttft is not None:
        res["ttft_ms"] = ttft
    if total is not None:
        res["total_ms"] = total
    return res


def pct(sorted_vals, p):
    """Nearest-rank percentile (p in 0..100) over a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _avg(vals):
    return (sum(vals) / len(vals)) if vals else 0.0


def _avg_key(results, key):
    return _avg([r[key] for r in results if r.get(key) is not None])


def run_step(op, base, domain, conc, duration, timeout, ctx, max_ops=0):
    """Run `conc` threads hammering `op` for `duration`s (or until max_ops, if >0)."""
    results = []            # list.append is atomic under CPython's GIL
    deadline = time.monotonic() + duration
    budget = threading.Semaphore(max_ops) if max_ops > 0 else None
    capped = {"hit": False}

    def worker():
        while time.monotonic() < deadline:
            if budget is not None and not budget.acquire(blocking=False):
                capped["hit"] = True
                break
            results.append(op(base, domain, timeout, ctx))

    threads = [threading.Thread(target=worker) for _ in range(conc)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0

    sent = len(results)
    ok = sum(1 for r in results if r["ok"])
    ok_lat = sorted(r["ms"] for r in results if r["ok"])
    errs = [r["err"] for r in results if r["err"]][:1]
    return {
        "conc": conc, "sent": sent, "ok": ok, "elapsed": elapsed,
        "rps": (sent / elapsed) if elapsed > 0 else 0.0,
        "err_pct": (100.0 * (sent - ok) / sent) if sent else 0.0,
        "xwork": sum(1 for r in results if r["xwork404"]), "capped": capped["hit"],
        "p50": pct(ok_lat, 50), "p90": pct(ok_lat, 90),
        "p95": pct(ok_lat, 95), "p99": pct(ok_lat, 99),
        "max": ok_lat[-1] if ok_lat else 0.0,
        "avg_llm": _avg_key(results, "llm_ms"), "avg_tts": _avg_key(results, "tts_ms"),
        "avg_ttft": _avg_key(results, "ttft_ms"), "avg_total": _avg_key(results, "total_ms"),
        "sample_err": errs[0] if errs else "",
    }


def run_scenario(name, op, base, domain, steps, duration, timeout, ctx,
                 show_xwork=False, show_timing=False, show_stream=False, max_ops=0):
    print("\n=== scenario: %s ===" % name)
    if show_stream:
        print("  latency columns (p50..max) = TTFA, time-to-first-audio (ms);"
              " ttft/total are averages")
    hdr = " conc   sent     ok   err%%    rps      p50     p90     p95     p99     max"
    if show_timing:
        hdr += "   srv_llm srv_tts"
    if show_stream:
        hdr += "   avg_ttft avg_total"
    if show_xwork:
        hdr += "   xwork404"
    print(hdr)
    print("-" * (len(hdr) + 2))
    worst_err = 0.0
    saw_xwork = 0
    for conc in steps:
        r = run_step(op, base, domain, conc, duration, timeout, ctx, max_ops)
        line = "%5d %6d %6d  %5.1f %7.1f  %7.0f %7.0f %7.0f %7.0f %7.0f" % (
            r["conc"], r["sent"], r["ok"], r["err_pct"], r["rps"],
            r["p50"], r["p90"], r["p95"], r["p99"], r["max"])
        if show_timing:
            line += "  %7.0f %7.0f" % (r["avg_llm"], r["avg_tts"])
        if show_stream:
            line += "  %8.0f %9.0f" % (r["avg_ttft"], r["avg_total"])
        if show_xwork:
            line += "  %9d" % r["xwork"]
        if r["capped"]:
            line += "  (capped@%d)" % max_ops
        print(line)
        if r["sample_err"]:
            print("        ^ sample error: %s" % r["sample_err"])
        worst_err = max(worst_err, r["err_pct"])
        saw_xwork += r["xwork"]
    if show_xwork and saw_xwork:
        print("\n  !! %d cross-worker 404s — sessions created on one worker were NOT"
              " visible to another." % saw_xwork)
        print("     Almost always means DATABASE_URL is unset/unreachable, so workers"
              " fell back to per-process memory.")
    elif show_xwork:
        print("\n  OK: no cross-worker 404s — workers are sharing session state (DB is live).")
    return worst_err


def main():
    ap = argparse.ArgumentParser(description="Stepped-concurrency load test (stdlib only).")
    ap.add_argument("--url", required=True, help="Base URL, e.g. https://staging.example.com")
    ap.add_argument("--scenario", default="health",
                    choices=["health", "session", "turn", "stream", "both", "all"])
    ap.add_argument("--steps", default="1,5,10,25,50,100",
                    help="Comma-separated concurrency levels (default: 1,5,10,25,50,100)")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="Seconds to hold each concurrency step (default: 10)")
    ap.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout (s)")
    ap.add_argument("--domain", default="physical_design", help="Resume domain for session/turn/stream")
    ap.add_argument("--max-turns", type=int, default=60, dest="max_turns",
                    help="turn/stream only: hard cap on paid calls PER STEP (default 60)")
    ap.add_argument("--insecure", action="store_true", help="Skip TLS cert verification")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    try:
        steps = [int(s) for s in args.steps.split(",") if s.strip()]
    except ValueError:
        print("--steps must be comma-separated integers", file=sys.stderr)
        return 2

    ctx = None
    if args.insecure and base.startswith("https"):
        ctx = ssl._create_unverified_context()

    print("Target: %s" % base)
    print("Steps:  %s  |  %.0fs per step  |  timeout %.0fs" % (steps, args.duration, args.timeout))

    do_turn = args.scenario in ("turn", "all")
    do_stream = args.scenario in ("stream", "all")
    if do_turn or do_stream:
        n_paid = (1 if do_turn else 0) + (1 if do_stream else 0)
        print("\n*** turn/stream scenarios call the REAL LLM + TTS — this COSTS MONEY. ***")
        print("    Up to %d calls/step x %d steps x %d paid scenario(s) = ~%d paid LLM+TTS calls max."
              % (args.max_turns, len(steps), n_paid, args.max_turns * len(steps) * n_paid))
        print("    429 / 5xx errors here usually mean PROVIDER RATE LIMITS, not app bugs.")

    worst = 0.0
    if args.scenario in ("health", "both", "all"):
        worst = max(worst, run_scenario("health", op_health, base, args.domain,
                                        steps, args.duration, args.timeout, ctx))
    if args.scenario in ("session", "both", "all"):
        worst = max(worst, run_scenario("session", op_session, base, args.domain,
                                        steps, args.duration, args.timeout, ctx,
                                        show_xwork=True))
    if do_turn:
        worst = max(worst, run_scenario("turn", op_turn, base, args.domain,
                                        steps, args.duration, args.timeout, ctx,
                                        show_timing=True, max_ops=args.max_turns))
    if do_stream:
        worst = max(worst, run_scenario("stream", op_stream, base, args.domain,
                                        steps, args.duration, args.timeout, ctx,
                                        show_stream=True, max_ops=args.max_turns))

    print("\nDone. Read the curve: where p95/p99 spikes or err%% climbs is your ceiling.")
    return 1 if worst > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
