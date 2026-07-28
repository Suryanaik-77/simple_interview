"""Full simulated interview: an LLM plays a realistic nervous fresher, the real
generate_greeting/generate_question drive the interviewer. Verifies behaviour and
meters cost (actual model + projected gpt-4o-mini production cost)."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"   # local key for gpt-4o-mini is dead
main.RUNTIME_CONFIG["qgen_model"] = MODEL

# ── meter every interviewer-side LLM call (greeting + questions + expected_points)
calls = []
_orig = main.call_llm
def metered(messages, model_id="", temperature=0.5, max_tokens=500):
    t, u = _orig(messages, model_id, temperature, max_tokens)
    calls.append((u["input_tokens"], u["output_tokens"], u["cost_usd"]))
    return t, u
main.call_llm = metered

# candidate uses bedrock DIRECTLY so it never pollutes the interviewer meter
CAND_PERSONA = (
    "You are Indhu, a nervous VLSI design-verification fresher in a voice interview. "
    "You did an async FIFO and a 32-bit ALU with UVM in Synopsys VCS. Answer in 2-4 "
    "spoken sentences. Be realistic: mostly partial/vague, occasionally correct, and "
    "sometimes honestly say you're not sure. Never sound like a textbook. "
    "Output ONLY the words you speak — no stage directions, no asterisks, no narration "
    "like *clears throat* or *pauses*. If asked something you don't know, just say so in words.")
def candidate_answer(q):
    msgs = [{"role": "system", "content": CAND_PERSONA},
            {"role": "user", "content": f"Interviewer just asked: {q}\nYour spoken answer:"}]
    t, _ = main._call_bedrock(msgs, MODEL, 0.85, 160)
    return t.strip()

sess = {"id": "fulltest", "mode": "mock", "turn": 0, "phase": "active", "obs_log": [], "conversation": [],
        "resume": {"domain": "design_verification", "level": "trained_fresher",
                   "candidate_name": "Eereddy Indhu", "email": "",
                   "tools": ["Synopsys VCS"], "skills": ["Verilog", "SystemVerilog", "UVM", "Functional Coverage"],
                   "key_projects": [{"name": "Asynchronous FIFO Verification", "description": "UVM TB, CDC corner cases"},
                                    {"name": "32-Bit Pipelined ALU Verification", "description": "UVM env, assertions, ref model"}],
                   "resume_text": "ECE 2025 graduate, VLSI DV trainee. Async FIFO and 32-bit pipelined ALU verified with UVM in Synopsys VCS.",
                   "years_experience": 0}}

print("=" * 80)
greet = main.generate_greeting(sess)
print(f"[RANJITHA greeting] {greet}\n")
q = greet  # generate_greeting already appended the greeting to conversation

N = 12
tags = []
for i in range(N):
    ans = candidate_answer(q)
    print(f"[Indhu] {ans}")
    res = main.generate_question(sess, ans)
    q = res.get("question", "")
    # main agent must now emit CLEAN questions (no tags)
    assert not any(t in q for t in ("[FOLLOWUP]", "[SCENARIO]", "[CONCEPT]", "[PROJECT]")), f"stray tag in: {q}"
    # tags are decided by the background classifier; call it synchronously here so the
    # per-turn label + the next turn's prev-is-followup check are deterministic in the test
    main.classify_question_tags(sess, q)
    last = sess["conversation"][-1]
    tag = "FOLLOWUP" if last.get("is_followup") else ("SCENARIO" if last.get("is_scenario") else "new")
    tags.append(f"{tag}/{last.get('qtype','?')}")
    print(f"[RANJITHA #{i+1} ({tag}, type={last.get('qtype','?')})] {q}\n")
    if res.get("should_end"):
        print("(interview ended)"); break

time.sleep(7)  # let background expected-points threads finish so their cost is counted

# ── behaviour checks ────────────────────────────────────────────────────────
print("=" * 80)
print("BEHAVIOUR CHECKS (tags decided by the background classifier agent)")
fu = [t.split("/")[0] == "FOLLOWUP" for t in tags]
sc = [t.split("/")[0] == "SCENARIO" for t in tags]
print(f"  opened on a NEW question (not follow-up): {not fu[0] if fu else True}")
two_in_row = any(fu[i] and fu[i+1] for i in range(len(fu)-1))
print(f"  never two follow-ups in a row: {not two_in_row}")
print(f"  at least one scenario asked: {any(sc)}")
print(f"  tag/type sequence: {tags}")

# ── question-type mix (same classification the app uses) ────────────────────
proj_names = [p["name"] for p in sess["resume"]["key_projects"]]
def mentions(q):
    ql = q.lower()
    for nm in proj_names:
        first = nm.split()[0].lower()
        if nm.lower() in ql or (len(first) >= 4 and first in ql):
            return True
    return False
counts = {"PROJECT": 0, "CONCEPT": 0, "SCENARIO": 0}
for e in sess["conversation"]:
    q = e.get("question", "")
    if not q or any(g in q.lower() for g in ("tell me about yourself", "introduce yourself")):
        continue
    t = e.get("qtype")  # verdict from the background classifier
    if t not in counts:
        t = "SCENARIO" if e.get("is_scenario") else ("PROJECT" if mentions(q) else "CONCEPT")
    counts[t] += 1
tot = sum(counts.values()) or 1
print(f"  MIX  PROJECT {counts['PROJECT']} ({100*counts['PROJECT']//tot}%) | "
      f"CONCEPT {counts['CONCEPT']} ({100*counts['CONCEPT']//tot}%) | "
      f"SCENARIO {counts['SCENARIO']} ({100*counts['SCENARIO']//tot}%)   target 35/45/20")
print("  standalone CONCEPT questions asked (no project name):")
for e in sess["conversation"]:
    q = e.get("question", "")
    if q and not e.get("is_followup") and not e.get("is_scenario") and not mentions(q) \
       and not any(g in q.lower() for g in ("tell me about yourself", "introduce yourself")):
        print(f"     • {q[:90]}")

# ── cost ────────────────────────────────────────────────────────────────────
in_tok = sum(c[0] for c in calls); out_tok = sum(c[1] for c in calls)
cost_actual = sum(c[2] for c in calls)
cost_4omini = sum(main._calc_llm_cost("gpt-4o-mini", c[0], c[1]) for c in calls)
nq = len([t for t in tags])
print("=" * 80)
print("COST")
print(f"  interviewer LLM calls   : {len(calls)}  (greeting + {nq} questions + expected-points)")
print(f"  tokens                  : in={in_tok:,}  out={out_tok:,}")
print(f"  ACTUAL ({MODEL.split('.')[-1]}): ${cost_actual:.4f}")
print(f"  PROJECTED on gpt-4o-mini (prod): ${cost_4omini:.4f}")
print(f"  per-interview @ gpt-4o-mini     : ~${cost_4omini:.4f}  (no prompt-cache credit applied)")
