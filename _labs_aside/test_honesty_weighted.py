"""Test: weighted expected-points + honest 'I didn't face it' handling."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main
main.RUNTIME_CONFIG["qgen_model"] = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
main.RUNTIME_CONFIG["eval_model"] = "us.anthropic.claude-sonnet-4-6"  # prod eval tier

RESUME = {"email": "", "domain": "design_verification", "level": "trained_fresher",
          "candidate_name": "Indhu", "tools": ["Synopsys VCS"], "skills": ["UVM", "SystemVerilog"],
          "key_projects": [{"name": "Asynchronous FIFO Verification", "description": "UVM TB, CDC"},
                           {"name": "32-Bit ALU Verification", "description": "UVM, assertions"}],
          "resume_text": "DV fresher, async FIFO + ALU with UVM/VCS.", "years_experience": 0}

META_Q = "How did you handle metastability between the two async clocks in your FIFO verification?"
META_EP = [{"point": "two-flop synchronizer on control signals", "weight": "core"},
           {"point": "gray-code pointers across the domains", "weight": "core"},
           {"point": "quantify MTBF / metastability window", "weight": "extra"}]
HONEST_ANS = ("Honestly, I didn't really face any metastability issue in my FIFO project. "
              "I used gray-code pointers for the read and write pointers, so it didn't come up. "
              "I didn't add any extra synchronizer stages myself.")

# ── PART 1: does the interviewer MOVE ON after an honest 'didn't face it'? ──────
print("=" * 78, "\nPART 1 — follow-up steering on an honest 'I didn't face it'\n", "=" * 78)
sess = {"id": "h1", "mode": "mock", "turn": 3, "phase": "active", "obs_log": [], "resume": RESUME,
        "conversation": [
            {"question": "Tell me about yourself", "answer": "I'm Indhu, did FIFO and ALU with UVM.", "turn": 0},
            {"question": "What is functional coverage?", "answer": "It measures how much of the intent we tested.", "turn": 1, "qtype": "CONCEPT"},
            {"question": META_Q, "answer": HONEST_ANS, "turn": 2, "qtype": "PROJECT", "expected_points": META_EP},
        ]}
res = main.generate_question(sess, HONEST_ANS)
nxt = res["question"]
print(f"\n[candidate] {HONEST_ANS}")
print(f"[next question] {nxt}")
drills = any(w in nxt.lower() for w in ("metastab", "synchroniz", "two-flop", "double flop"))
print(f"\n==> moved ON (did NOT keep drilling metastability): {not drills}")

# ── PART 2: does the EVALUATOR avoid penalizing the honest answer? ──────────────
print("\n" + "=" * 78, "\nPART 2 — scoring: honest 'didn't face it' must not be penalized\n", "=" * 78)
conv = [
    {"question": "What is functional coverage?", "answer": "It measures how much of the design intent we've exercised, using covergroups and coverpoints; different from code coverage.", "turn": 0, "expected_points": [{"point": "measures design intent coverage", "weight": "core"}, {"point": "covergroups/coverpoints", "weight": "core"}]},
    {"question": "Why is SystemVerilog preferred over Verilog for verification?", "answer": "It has OOP, constrained randomization, assertions and functional coverage which Verilog lacks.", "turn": 1, "expected_points": [{"point": "OOP + randomization", "weight": "core"}, {"point": "assertions/coverage", "weight": "core"}]},
    {"question": META_Q, "answer": HONEST_ANS, "turn": 2, "expected_points": META_EP},  # honest didn't-face-it
    {"question": "What is a UVM driver vs monitor?", "answer": "Driver drives stimulus onto the interface, monitor passively samples and sends transactions to the scoreboard.", "turn": 3, "expected_points": [{"point": "driver drives, monitor observes", "weight": "core"}]},
    {"question": "How did you check results in your ALU testbench?", "answer": "A scoreboard compared DUT output against a reference model for each operation.", "turn": 4, "expected_points": [{"point": "scoreboard vs reference model", "weight": "core"}]},
    {"question": "What is constrained-random verification?", "answer": "You randomize stimulus within constraints so you hit many cases automatically instead of writing directed tests.", "turn": 5, "expected_points": [{"point": "randomize within constraints", "weight": "core"}]},
    {"question": "What are the key UVM phases?", "answer": "build, connect, run and the report phases; build is top-down, connect bottom-up.", "turn": 6, "expected_points": [{"point": "build/connect/run phases", "weight": "core"}]},
    {"question": "How did you verify FIFO overflow and underflow?", "answer": "Separate sequences that wrote past full and read when empty, and checked the flags asserted.", "turn": 7, "expected_points": [{"point": "drive full/empty corner cases", "weight": "core"}]},
]
sess2 = {"id": "h2", "mode": "mock", "phase": "ended", "obs_log": [], "resume": RESUME, "conversation": conv}
ev = main.evaluate_interview(sess2)
pq = ev.get("per_question", [])
print(f"\noverall_score={ev.get('overall_score')}  status={ev.get('status')}")
# find the metastability question entry
for item in pq:
    if "metastab" in (item.get("question", "").lower()):
        print("\nMETASTABILITY question scoring:")
        print(f"  score        : {item.get('score')} /10")
        print(f"  missing_points: {item.get('missing_points')}")
        print(f"  comment      : {item.get('comment')}")
print("\n(If honesty carve-out works: score is NOT tanked and missing_points does NOT list")
print(" the synchronizer/gray-code points as a knowledge gap held against them.)")
