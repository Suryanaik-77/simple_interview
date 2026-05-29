#!/usr/bin/env python3
"""
loadtest.py — stepped-concurrency load test for the Simple Interview API.

Pure standard library (urllib + threading), so it runs on old Pythons (3.6+)
with zero installs. It hits a remote staging URL, ramps concurrency through a
series of steps, and prints latency percentiles + throughput + error rate per
step so you can see how the service behaves as load grows.

SCENARIOS (all are CHEAP — they never touch the LLM/STT/TTS providers, so they
cost nothing and won't hit AI rate limits):

  health   GET /health
           Pure server-capacity probe. Measures how the 4-worker process pool
           handles raw request concurrency. No side effects.

  session  POST /api/create-session  ->  GET /api/get-session  ->  POST /api/end-session
           One full lifecycle per operation. This is the important correctness
           probe: create lands on one worker, the GET must find the session on
           (possibly) ANOTHER worker. If workers don't share state via Postgres
           (e.g. DATABASE_URL unset), the GET returns 404 — reported as the
           "xwork404" column. The trailing end-session also exercises
           delete-on-end, so it cleans up after itself and won't grow
           active_sessions. The create payload sends a pre-parsed JSON resume,
           so the server skips resume parsing (no LLM call).

USAGE
  python3 loadtest.py --url https://staging.example.com --scenario health
  python3 loadtest.py --url https://staging.example.com --scenario session
  python3 loadtest.py --url https://staging.example.com --scenario both \
      --steps 1,5,10,25,50,100 --duration 15

  # self-signed / mismatched cert on staging:
  python3 loadtest.py --url https://staging.example.com --insecure ...

Read the table like a growth curve: as 'conc' rises, watch p95/p99 and err%.
The "knee" — where latency jumps sharply or errors appear — is your ceiling.
"""

import argparse
import json
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request


def do_request(method, url, body=None, timeout=30, ctx=None):
    """Issue one HTTP request. Returns (status, latency_ms, body_bytes, error).
    status is the HTTP code (incl. 4xx/5xx); error is set only on transport
    failures (timeout, connection refused, DNS, etc)."""
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
        latency = (time.monotonic() - t0) * 1000.0
        return resp.getcode(), latency, payload, None
    except urllib.error.HTTPError as e:
        # 4xx/5xx still arrived — a real HTTP response, not a transport error.
        latency = (time.monotonic() - t0) * 1000.0
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, latency, payload, None
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000.0
        return None, latency, b"", str(e)


def op_health(base, domain, timeout, ctx):
    """One /health hit. Returns a result dict."""
    status, ms, _body, err = do_request("GET", base + "/health", None, timeout, ctx)
    ok = (status == 200)
    return {"ok": ok, "ms": ms, "xwork404": False, "err": err, "status": status}


def op_session(base, domain, timeout, ctx):
    """Full create -> get -> end lifecycle. Returns a result dict.
    Sends a pre-parsed JSON resume so the server does NOT call the LLM."""
    t0 = time.monotonic()
    resume = json.dumps({
        "candidate_name": "Load Test",
        "domain": domain,
        "level": "trained_fresher",
        "years_experience": 1,
        "email": "",            # empty -> server skips candidate_history writes
        "skills": [], "tools": [], "key_projects": [],
    })
    create_body = {"resume_text": resume, "mode": "mock", "domain": domain}

    status, _ms, payload, err = do_request(
        "POST", base + "/api/create-session", create_body, timeout, ctx)
    if err or status != 200:
        return {"ok": False, "ms": (time.monotonic() - t0) * 1000.0,
                "xwork404": False, "err": err or ("create %s" % status), "status": status}

    try:
        sid = json.loads(payload.decode("utf-8")).get("session_id")
    except Exception as e:
        return {"ok": False, "ms": (time.monotonic() - t0) * 1000.0,
                "xwork404": False, "err": "bad create json: %s" % e, "status": status}

    # The cross-worker test: this GET may hit a different worker than the POST.
    gstatus, _gms, _gp, gerr = do_request(
        "GET", base + "/api/get-session?session_id=" + str(sid), None, timeout, ctx)
    xwork404 = (gstatus == 404)
    get_ok = (gstatus == 200)

    # Clean up (also exercises delete-on-end). Best-effort; don't fail the op on it.
    do_request("POST", base + "/api/end-session", {"session_id": sid}, timeout, ctx)

    total = (time.monotonic() - t0) * 1000.0
    if gerr or not get_ok:
        return {"ok": False, "ms": total, "xwork404": xwork404,
                "err": gerr or ("get %s" % gstatus), "status": gstatus}
    return {"ok": True, "ms": total, "xwork404": False, "err": None, "status": 200}


def pct(sorted_vals, p):
    """Nearest-rank percentile (p in 0..100) over a pre-sorted list."""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def run_step(op, base, domain, conc, duration, timeout, ctx):
    """Run `conc` threads hammering `op` for `duration` seconds. Returns aggregates."""
    results = []            # list.append is atomic under CPython's GIL
    deadline = time.monotonic() + duration

    def worker():
        while time.monotonic() < deadline:
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
    xwork = sum(1 for r in results if r["xwork404"])
    ok_lat = sorted(r["ms"] for r in results if r["ok"])
    errs = [r["err"] for r in results if r["err"]][:1]   # sample one error string
    return {
        "conc": conc, "sent": sent, "ok": ok, "elapsed": elapsed,
        "rps": (sent / elapsed) if elapsed > 0 else 0.0,
        "err_pct": (100.0 * (sent - ok) / sent) if sent else 0.0,
        "xwork": xwork,
        "p50": pct(ok_lat, 50), "p90": pct(ok_lat, 90),
        "p95": pct(ok_lat, 95), "p99": pct(ok_lat, 99),
        "max": ok_lat[-1] if ok_lat else 0.0,
        "sample_err": errs[0] if errs else "",
    }


def run_scenario(name, op, base, domain, steps, duration, timeout, ctx, show_xwork):
    print("\n=== scenario: %s ===" % name)
    hdr = " conc   sent     ok   err%%    rps      p50     p90     p95     p99     max"
    if show_xwork:
        hdr += "   xwork404"
    print(hdr)
    print("-" * (len(hdr) + 2))
    worst_err = 0.0
    saw_xwork = 0
    for conc in steps:
        r = run_step(op, base, domain, conc, duration, timeout, ctx)
        line = "%5d %6d %6d  %5.1f %7.1f  %7.0f %7.0f %7.0f %7.0f %7.0f" % (
            r["conc"], r["sent"], r["ok"], r["err_pct"], r["rps"],
            r["p50"], r["p90"], r["p95"], r["p99"], r["max"])
        if show_xwork:
            line += "  %9d" % r["xwork"]
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
    ap.add_argument("--scenario", default="health", choices=["health", "session", "both"])
    ap.add_argument("--steps", default="1,5,10,25,50,100",
                    help="Comma-separated concurrency levels (default: 1,5,10,25,50,100)")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="Seconds to hold each concurrency step (default: 10)")
    ap.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout (s)")
    ap.add_argument("--domain", default="physical_design", help="Resume domain for session scenario")
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

    worst = 0.0
    if args.scenario in ("health", "both"):
        worst = max(worst, run_scenario("health", op_health, base, args.domain,
                                        steps, args.duration, args.timeout, ctx, False))
    if args.scenario in ("session", "both"):
        worst = max(worst, run_scenario("session", op_session, base, args.domain,
                                        steps, args.duration, args.timeout, ctx, True))

    print("\nDone. Read the curve: where p95/p99 spikes or err%% climbs is your ceiling.")
    return 1 if worst > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
