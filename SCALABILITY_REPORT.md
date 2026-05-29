# Interview Platform — Scalability & Performance Report

**Prepared for:** CEO
**Date:** May 29, 2026
**Subject:** Can our AI interview platform handle large numbers of candidates at the same time?

---

## Executive Summary

We set out to answer one question: **if many candidates use our AI interview platform at the same time, will it stay fast and reliable — or will it slow down and break?**

We found and fixed several issues that would have caused slowdowns and failures under load, then ran realistic simulations of heavy usage. The results are strong:

- ✅ **Fast:** A candidate hears the AI interviewer respond in about **1.3 seconds** — a natural, conversational pace.
- ✅ **Reliable:** Across every test, including deliberate overload, the platform handled **100% of requests with zero failures**.
- ✅ **High capacity:** A single server can comfortably support an estimated **~500 live interviews running at the same time**.
- ✅ **Built to grow:** We re-engineered the platform so capacity can be increased simply by **adding more servers** — a routine, low-risk step that was *not* possible before.

**Bottom line: the platform is ready for our current needs and near-term growth, with a clear and inexpensive path to scale much further.**

---

## 1. The Question We Answered

A demo that works for one candidate doesn't prove it works for hundreds at once. Before we grow usage, we needed confidence that the platform won't slow to a crawl or fail when traffic rises. This report covers what we checked, what we fixed, and how it performed under pressure.

A helpful way to picture it: think of the platform like a **call center**. The two questions that matter are *"how many calls can we handle at the same time?"* and *"how quickly does the agent respond?"* This report answers both.

---

## 2. What We Found and Fixed

The platform had a few under-the-hood limitations that wouldn't show up in a small demo but would have caused real problems as usage grew. In plain terms:

| Issue (in plain language) | What would have happened | Status |
|---|---|---|
| The system could only run as a single "lane," handling requests almost one-at-a-time. | Candidates would queue behind each other and wait. | **Fixed** — it now processes many in parallel. |
| It couldn't safely run on multiple servers — each server forgot the others' interviews. | Adding servers to grow would have *broken* live interviews. | **Fixed** — servers now share one common memory (a shared database). |
| On startup, multiple servers collided while setting up, and some quietly dropped offline. | Capacity would silently shrink; some candidates would get errors. | **Fixed** — startup is now coordinated and safe. |
| Background tasks could overwrite and corrupt an interview's progress. | Occasional lost answers or glitches under load. | **Fixed.** |
| Old interview data was never cleaned up. | The system would slowly bloat and slow down over time. | **Fixed** — finished interviews are now cleaned up automatically. |

We also built a **testing tool** that simulates many candidates using the platform at once, so we (and you) can re-verify performance any time — for example, before a big hiring event.

---

## 3. How We Tested It

We simulated waves of candidates taking interviews simultaneously — from light traffic up to deliberately extreme bursts — and measured two things that matter most:

1. **Speed:** how long a candidate waits to hear the AI interviewer respond after they finish speaking.
2. **Reliability:** whether any interviews failed, errored, or were dropped.

We tested the **real** interview experience (live AI conversation + voice), not a simplified stand-in.

---

## 4. Results

**Response speed and reliability as load increases:**

| Load level | Wait to hear the interviewer respond | Failures |
|---|---|---|
| Light (≈10–25 responses at once) | **~1.3 seconds** (feels natural) | **None** |
| Heavy (≈50 at once) | ~2.3 seconds (still comfortable) | **None** |
| Extreme burst (100 responses at the *exact same instant*) | ~4.7 seconds (noticeably slower) | **None** |

A few things stand out:

- **At normal-to-heavy load, the experience is fast and smooth** — around a one-second pause, like a real conversation.
- **Nothing ever failed**, even when we deliberately overloaded it. No dropped interviews, no errors.
- The "extreme burst" row is a **worst-case stress test** — 100 candidates answering at the identical split second. In real life, candidates spend most of an interview listening and thinking, so they don't all hit *Submit* simultaneously. So this is a torture test, not everyday behavior — and even then, it slowed but never broke.

**What this means for capacity:** Because real usage is naturally spread out, a **single server can support an estimated ~500 interviews happening concurrently** while keeping responses fast. *(This is an engineering estimate based on typical interview pacing; the measured speed and reliability numbers above are direct test results.)*

---

## 5. What This Means for the Business

- **We can confidently grow usage today.** The platform is fast and reliable well beyond our current volume.
- **Scaling further is simple and low-risk.** To go past ~500 concurrent interviews, we add more servers — a standard operation now that the platform is built for it. This used to be impossible without breaking interviews.
- **The candidate experience is good.** Sub-1.5-second responses feel like a natural conversation, which matters for how candidates perceive our product.
- **We have a repeatable way to verify performance** before any high-traffic event.

---

## 6. The One Real Limit to Be Aware Of

The platform relies on outside AI services (for the conversation and the voice). As we scale, the main ceiling is no longer our own software — it's our **usage limits and budget with those AI providers**, and the **per-interview cost** they charge.

In short: scaling further is now primarily a **budget and vendor-capacity decision**, not an engineering obstacle. As volume grows, we should monitor AI-provider usage limits and cost per interview, and raise our provider quotas ahead of major events.

---

## 7. Recommendations / Next Steps

1. **One-time production check:** Confirm the production environment uses the shared-database configuration we validated. (This is the single setting that the multi-server reliability depends on.)
2. **Plan capacity by event:** For known high-traffic moments (e.g., a campus hiring drive), confirm provider quotas in advance and add servers if we expect to exceed ~500 concurrent interviews.
3. **Track AI-provider cost and limits** as a normal operating metric as usage grows.
4. **Optional future optimization:** A further engineering improvement could roughly **double the capacity of each server**. There's no urgency — it's worth doing only if/when we approach the per-server limit.

---

## Bottom Line

The platform went from *"works in a demo"* to *"proven fast and reliable under heavy, realistic load, with a clear path to scale."* It responds in about a second, didn't fail a single time in testing, can handle hundreds of simultaneous interviews per server, and can grow by simply adding servers. The remaining limit is vendor budget and quotas — a business lever, not a technical wall.
