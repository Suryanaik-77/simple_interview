"""
Question-gen test on Mahesh Veeraboina's REAL resume.
Scenarios:
  A. OpenAI  first-time   (baseline)
  B. Claude  first-time   (quality / brevity / tagging / prefix size)
  C. Claude  RETURNING    (2 prior sessions injected -> bigger stable prefix +
                           no-repeat check + does caching finally fire?)

Run on EC2:  /home/ubuntu/venv311/bin/python _labs_aside/qgen_compare.py
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

OPENAI_MODEL = "gpt-4.1-mini"
CLAUDE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

RESUME_TEXT = (
    "PROFESSIONAL SUMMARY: Physical Design Engineer with industry experience in the "
    "complete RTL-to-GDSII implementation flow at 28nm. Worked on production SoC blocks "
    "up to 350,000 standard cells (24 hard macros, 5 independent clock domains), with "
    "hands-on expertise in MCMM (Multi-Corner Multi-Mode) static timing analysis, SPEF "
    "back-annotation via Synopsys StarRC parasitic extraction, OCV/AOCV derating, "
    "post-route ECO implementation, and DRC/LVS physical signoff. Proficient in Cadence "
    "Innovus (P&R), Synopsys PrimeTime (STA signoff), Synopsys StarRC (extraction), and "
    "Cadence Voltus (IR drop / PGV). Strong TCL/TCSH/Makefile scripting for full PNR "
    "flow automation.\n\n"
    "WORK EXPERIENCE:\n"
    "Physical Design Engineer, SemiconOS (May 2026-Present): Independently executed "
    "end-to-end RTL-to-GDSII for three 28nm block-level designs - Tile (302,511 cells, "
    "18 hard macros, 1.7ns), RPTOP (69K gates, 6 macros, 1.8ns), and ALU (428 gates) - "
    "all DRC-clean GDSII. Post-route STA in PrimeTime with SPEF back-annotation from "
    "StarRC. MCMM timing across multiple PVT corners and modes. Post-route ECOs to "
    "resolve setup/hold via PrimeTime-guided incremental fixes. Voltus IR-drop / PGV on "
    "Tile; resolved EM violations in critical power straps. Automated TCL/TCSH flows for "
    "constraint loading, SPEF annotation, report generation, DRC batch checking.\n"
    "Physical Design Engineer Intern, RiseTime Semiconductors (June 2025-May 2026): Full "
    "PNR of the Iguana SoC block (~350,000 std cells, 24 hard macros, 5 independent clock "
    "domains) at 28nm in Innovus - floorplan through DRC/LVS. MCMM STA across PVT corners; "
    "StarRC SPEF into PrimeTime. OCV/AOCV derating across the 5-domain multi-clock design. "
    "Converged multi-clock CTS across 5 clock domains meeting skew/insertion-delay targets; "
    "post-route ECOs. Voltus power-grid integrity (IR drop, EM) and multi-power-domain "
    "floorplan. End-to-end Makefile+TCL automation (floorplan->place->CTS->route->SPEF->STA).\n\n"
    "KEY PROJECTS:\n"
    "Tile - 302,511 std cells, 18 hard macros, 1.7ns; MCMM STA, StarRC SPEF, OCV/AOCV, "
    "post-route ECO in PrimeTime; Voltus IR drop and PGV signoff.\n"
    "Iguana (industry) - ~350,000 std cells, 24 macros, 5 clock domains; full RTL-to-GDSII, "
    "MCMM STA, SPEF back-annotation, OCV/AOCV, post-route ECO, DRC/LVS signoff prep, "
    "Makefile/TCL automation.\n"
    "RPTOP - 69,000-gate block, 6 macros, 1.8ns; full PNR, congestion resolved, timing "
    "closed via post-route ECO; DRC-clean GDSII with PrimeTime STA.\n"
    "ALU - 428-gate, Genus synthesis; SDC authoring (primary, generated & virtual clocks "
    "at 2ns); optimized for timing/area; formal equivalence via Genus QoR.\n\n"
    "SKILLS: RTL-to-GDSII, floorplanning, power planning, macro/std-cell placement, CTS, "
    "detailed routing, timing closure, DRC/LVS signoff at 28nm. STA, MCMM, SPEF "
    "back-annotation, OCV/AOCV derating, setup/hold fixing, post-route ECO, SDC authoring, "
    "multi-clock-domain analysis, PVT corners. DRC/LVS/antenna, PGV, IR drop, EM. Multi "
    "power-domain, congestion mgmt, hierarchical design. TCL/TCSH/Makefile automation. "
    "Tools: Innovus, Genus, PrimeTime, StarRC, Voltus, Formality."
)

def fresh_session():
    return {
        "resume": {
            "candidate_name": "Mahesh Veeraboina",
            "level": "experienced_junior",
            "domain": "physical_design",
            "years_experience": 1,
            "tools": ["Innovus", "PrimeTime", "StarRC", "Voltus", "Genus", "Formality"],
            "skills": ["RTL2GDSII", "MCMM STA", "multi-clock CTS", "OCV/AOCV",
                       "post-route ECO", "IR drop / EM (Voltus)", "TCL automation"],
            "key_projects": [
                {"name": "Iguana", "description": "350k-cell SoC block, 5 clock domains, MCMM STA (industry)"},
                {"name": "Tile", "description": "302k-cell block, 1.7ns, Voltus IR-drop/PGV signoff"},
                {"name": "RPTOP", "description": "69k-gate block, congestion + ECO timing closure"},
                {"name": "ALU", "description": "428-gate Genus synthesis + SDC authoring"},
            ],
            "resume_text": RESUME_TEXT,
            "email": "",
        },
        "conversation": [],
        "turn": 0,
    }

# Two prior sessions of questions (returning-candidate fixture) — full question data.
PREV_SESSIONS = [
    {"projects": [{"name": "Iguana"}, {"name": "Tile"}],
     "questions_asked": [
        "Good morning, tell me about yourself.",
        "On the Iguana block, how did you converge CTS across the 5 clock domains?",
        "How did you set up the MCMM view configuration for Iguana across PVT corners?",
        "What was your approach to OCV/AOCV derating on the multi-clock design?",
        "How did you back-annotate the StarRC SPEF into PrimeTime for post-route STA?",
        "Walk me through a specific hold violation you fixed with a post-route ECO.",
        "On Tile, how did you handle IR drop in Voltus at high utilization?",
        "How did you generate the PGV and what did the Voltus report tell you?",
        "What made the Tile floorplan tricky with 18 hard macros?",
        "How did your Makefile/TCL flow handle the floorplan-to-STA iteration?",
     ]},
    {"projects": [{"name": "RPTOP"}, {"name": "ALU"}],
     "questions_asked": [
        "Welcome back. Let's continue.",
        "On RPTOP, how did you resolve the congestion you mentioned?",
        "What routing-layer strategy did you use to close timing on RPTOP at 1.8ns?",
        "For the ALU, how did you author the SDC for the generated and virtual clocks?",
        "How did you verify formal equivalence with Genus QoR on the ALU?",
        "What was the biggest setup-timing challenge on RPTOP and how did you fix it?",
        "How did you decide macro placement for the 6 macros on RPTOP?",
        "Explain how you validated DRC-cleanliness before signing off RPTOP GDSII.",
     ]},
]

ANSWERS = [
    ("Sure. I'm a physical design engineer, about a year of industry experience, all at "
     "28nm. I've taken blocks from RTL to GDSII — the biggest was Iguana, a 350k-cell SoC "
     "block with 5 clock domains — plus Tile, RPTOP and an ALU synthesis project."),
    ("On Iguana I owned the full PNR. The hard part was converging multi-clock CTS across "
     "5 independent clock domains and hitting skew and insertion-delay targets, then "
     "closing setup and hold with post-route ECOs while staying DRC-clean."),
    ("Yeah, MCMM. I just set up the corners and modes and ran the standard analysis until "
     "timing was clean. Nothing unusual."),
    ("Honestly I don't remember the exact derating numbers we used for AOCV, my lead set "
     "those up. I mostly ran the flow."),
    ("On Tile I ran Voltus for IR drop at high utilization and found hotspots on the power "
     "straps, so I widened critical straps and fixed the EM violations, then re-ran PGV to "
     "confirm the grid was robust."),
    ("For post-route ECOs I used PrimeTime-guided incremental fixes — pulled the violating "
     "paths, inserted buffers or resized cells for hold, and re-ran STA, keeping everything "
     "inside the placement and routing constraints so it stayed DRC-clean."),
    ("Yeah that whole flow is pretty routine for me now, I've done it across all three "
     "blocks."),
]

# Rough flag (test harness only — final call is made by reading the questions):
# a question that both names a project AND reads like a theory check is the
# "hybrid" we banned. Not used in interview logic, just to spotlight candidates.
_CONC_HINTS = ("why ", "what is ", "what's ", "what are ", "what causes",
               "how does ", "explain ", "difference between", "trade-off", "tradeoff",
               "purpose of", "what does ", "when would you use", "meaning of")
def _looks_conceptual(q):
    ql = q.lower()
    return any(h in ql for h in _CONC_HINTS)

def run_model(model_id, label, returning=False):
    print(f"\n{'='*78}\n{label}  ({model_id}){'  [RETURNING]' if returning else ''}\n{'='*78}")
    proj_names = [p["name"] for p in fresh_session()["resume"]["key_projects"]]
    def which(q):
        ql = q.lower()
        hits = [n for n in proj_names if n.lower() in ql]
        return ",".join(hits) or "-"
    prev_qs = [q for s in PREV_SESSIONS for q in s["questions_asked"]] if returning else []

    session = fresh_session()
    if returning:
        session["resume"]["email"] = "maheshv.be25@uceou.edu"
        main.get_candidate_previous = lambda _e: PREV_SESSIONS  # inject fixture (no DB)
    rows = []
    for turn in range(len(ANSWERS)):
        session["turn"] = turn
        messages = main.build_interview_prompt(session)
        sys_tokens = main._estimate_tokens(messages[0]["content"], model_id) if turn == 0 else None
        t0 = time.time()
        text, usage = main.call_llm(messages, model_id=model_id, temperature=0.5, max_tokens=500)
        ms = round((time.time() - t0) * 1000)
        cr, cc = usage.get("cache_read_input_tokens", 0), usage.get("cache_creation_input_tokens", 0)
        up = text.upper()
        scen = "[SCENARIO]" in up[:40]
        clean = text.replace("[FOLLOWUP]", "").replace("[SCENARIO]", "").strip()
        words = len(clean.split())
        tagged = "[FOLLOWUP]" in up[:40]
        proj = which(clean)
        # question type: scenario tag wins; else project-named = PROJECT; else CONCEPT
        qtype = "SCEN" if scen else ("PROJ" if proj != "-" else "CONC")
        # the rule under test: a CONCEPT check must NOT name a project (no hybrid)
        blend = (not scen) and (not tagged) and (proj != "-") and _looks_conceptual(clean)
        repeat = any(pq.lower()[:30] in clean.lower() or clean.lower()[:30] in pq.lower()
                     for pq in prev_qs if len(pq) > 25)
        rows.append({"turn": turn, "words": words, "out": usage["output_tokens"],
                     "cr": cr, "cc": cc, "cost": usage["cost_usd"], "ms": ms,
                     "tag": tagged, "scen": scen, "type": qtype, "proj": proj,
                     "blend": blend, "repeat": repeat})
        if turn == 0:
            print(f"  [stable system-prefix ~= {sys_tokens} tokens (Haiku cache floor = 4096)]")
        print(f"\n  --- Turn {turn} [{qtype}] ({words}w out={usage['output_tokens']}tok cr={cr} cc={cc} "
              f"${usage['cost_usd']:.5f} {ms}ms proj={rows[-1]['proj']}"
              f"{' FOLLOWUP' if tagged else ''}{' SCENARIO' if scen else ''}"
              f"{' <<BLEND?' if blend else ''}{' REPEAT!' if repeat else ''}) ---")
        print(f"  Q: {clean}")
        # Mirror the app's submit path: it strips the tags and stores the flags.
        session["conversation"].append({"question": clean, "answer": ANSWERS[turn],
                                        "is_scenario": scen, "is_followup": tagged})
    return rows

def summarize(label, rows):
    tot = sum(r["cost"] for r in rows)
    avgw = sum(r["words"] for r in rows) / len(rows)
    tags = sum(1 for r in rows if r["tag"])
    reps = sum(1 for r in rows if r["repeat"])
    cr_hit = any(r["cr"] > 0 for r in rows)
    nproj = sum(1 for r in rows if r["type"] == "PROJ")
    nconc = sum(1 for r in rows if r["type"] == "CONC")
    nscen = sum(1 for r in rows if r["type"] == "SCEN")
    blends = sum(1 for r in rows if r["blend"])
    print(f"\n{label}: total=${tot:.5f} avg_words/q={avgw:.0f} tagged_followups={tags} "
          f"repeats={reps} cache_read_ever={cr_hit}")
    print(f"   mix: PROJ={nproj} CONC={nconc} SCEN={nscen}  hybrid_blend_flags={blends}")
    print("   type/turn: " + " -> ".join(r["type"] for r in rows))
    print("   proj/turn: " + " -> ".join(r["proj"] for r in rows))

if __name__ == "__main__":
    print("Live qgen_model:", main.RUNTIME_CONFIG.get("qgen_model"))
    a = run_model(OPENAI_MODEL, "OPENAI first-time")
    b = run_model(CLAUDE_MODEL, "CLAUDE first-time")
    c = run_model(CLAUDE_MODEL, "CLAUDE returning", returning=True)
    print(f"\n\n{'#'*78}\nSUMMARY\n{'#'*78}")
    summarize("OPENAI  first-time", a)
    summarize("CLAUDE  first-time", b)
    summarize("CLAUDE  returning ", c)
    print("\ntagged_followups: how many questions carried [FOLLOWUP]. repeats: questions "
          "that re-ask a previous-session question (returning only; should be 0).")
    print("cache_read_ever: True only if the stable prefix cleared 4096 and Bedrock cached it.")
