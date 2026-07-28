"""Drive the live question-generation path against the new prompts, offline.
No DB, no server — just build a session and call generate_greeting/generate_question."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main
# Local OPENAI key is dead; override qgen model for THIS TEST ONLY (not editing source/config)
main.RUNTIME_CONFIG["qgen_model"] = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

def make_session(level, domain, name, tools, skills, projects, resume_text, years):
    return {
        "id": "test", "mode": "mock", "turn": 0, "phase": "active",
        "conversation": [], "obs_log": [],
        "resume": {
            "candidate_name": name, "email": "",  # empty => no DB / no returning block
            "level": level, "domain": domain, "tools": tools, "skills": skills,
            "key_projects": projects, "resume_text": resume_text,
            "years_experience": years, "is_vlsi_suitable": True,
        },
    }

def run(title, session, answers):
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)
    greet = main.generate_greeting(session)
    print(f"\n[GREETING] {greet}")
    # seed the greeting as the first assistant turn so answers attach correctly
    session["conversation"].append({"question": greet, "answer": None, "turn": 0})
    for i, ans in enumerate(answers):
        print(f"\n[CANDIDATE] {ans}")
        res = main.generate_question(session, ans)
        q = res.get("question", "")
        # recover tags for display from the stored entry
        last = session["conversation"][-1]
        tag = ""
        if last.get("is_followup"): tag += " [FOLLOWUP]"
        if last.get("is_scenario"): tag += " [SCENARIO]"
        print(f"[RANJITHA]{tag} {q}")
        if res.get("should_end"):
            print("  (interview ended)"); break

# ---- DV fresher (trained_fresher) ----
dv = make_session(
    "trained_fresher", "design_verification", "Eereddy Indhu",
    ["Synopsys VCS"], ["Verilog", "SystemVerilog", "UVM", "Functional Verification"],
    [{"name": "Asynchronous FIFO Verification", "description": "UVM testbench for async FIFO, CDC corner cases."},
     {"name": "32-Bit Pipelined ALU Verification", "description": "UVM env with assertions and reference model."}],
    "VLSI DV trainee. Projects: async FIFO verification and 32-bit pipelined ALU verification using SystemVerilog, UVM, VCS.",
    0)
run("DV — trained fresher (should OPEN EASY, Indian voice, ramp, no tunnel)", dv, [
    "Good afternoon. I'm Indhu, ECE 2025 graduate. I did VLSI DV training and built two projects — an asynchronous FIFO and a 32-bit pipelined ALU, both verified with UVM in Synopsys VCS.",
    "SystemVerilog has OOP and constrained randomization, so it is better for verification than Verilog.",  # decent easy answer
    "Umm... coverage is like checking how much we tested.",  # vague -> expect ONE followup then move
    "In my FIFO I used two agents for read and write clock domains.",
])

# ---- PD senior ----
pd = make_session(
    "experienced_senior", "physical_design", "Ravi Kumar",
    ["ICC2", "PrimeTime", "StarRC"], ["Floorplanning", "CTS", "Timing Closure", "STA"],
    [{"name": "7nm CPU block PnR", "description": "Full PnR and timing closure of a 7nm CPU block, multi-Vt."}],
    "6 years physical design. Closed 7nm CPU blocks, floorplan to signoff, ICC2 + PrimeTime, MCMM.",
    6)
run("PD — senior (should OPEN AT MEDIUM/trade-off, skeptical voice)", pd, [
    "I'm Ravi, 6 years in physical design. I closed timing on a 7nm CPU block, full flow from floorplan to signoff using ICC2 and PrimeTime with MCMM.",
    "For hold violations I insert buffers on the fast paths and use useful skew where it helps.",
])

print("\n" + "=" * 78)
print("done")
