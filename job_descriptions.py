"""Per-domain job descriptions that scope the interview.

The interviewer draws its CONCEPT and SCENARIO topics from the role's required
skills/responsibilities here — covering the role even where the candidate's
résumé is silent — instead of keying off the candidate's self-listed skills.
The candidate's own projects are still used for PROJECT-type questions.

Keyed by the same domain slugs the app uses: physical_design,
design_verification, analog_layout.
"""

JOB_DESCRIPTIONS = {
    "physical_design": {
        "role": "Physical Design Engineer",
        "responsibilities": [
            "Drive frontend-to-backend implementation RTL → GDSII: synthesis, floorplanning, placement, CTS, routing, signoff",
            "Timing closure through physical synthesis and P&R tools; streamline timing signoff criteria, methodologies and flows",
            "Clock distribution and CTS for 1 GHz+ designs; scan reordering and placement optimization",
            "Routing, timing and SI analysis/closure; ECO (both timing and functional)",
            "Conceptual/theory-level understanding of DRC, LVS, ERC physical-verification checks",
            "Complete design cycle with minimal supervision; mentor small teams at senior level",
        ],
        "skills": [
            "FinFET technology", "extraction & STA methodology", "CAD automation",
            "DFT", "logic synthesis & equivalence checking",
            "basic SoC architecture and Verilog", "Perl", "Tcl", "Make",
        ],
        "tools": ["Synopsys ICC2", "Synopsys DC", "Cadence Genus", "Cadence Innovus"],
    },
    "design_verification": {
        "role": "Design Verification Engineer",
        "responsibilities": [
            "Analyze design specifications and understand verification requirements",
            "Develop verification test plans based on design specifications",
            "Identify, define, write, and execute directed and constrained-random test cases",
            "Build reusable SystemVerilog/UVM testbenches, including transactions, sequences, sequencers, drivers, monitors, agents, environments, and scoreboards",
            "Develop reusable UVM verification components",
            "Write SystemVerilog Assertions (SVA) to verify design functionality and detect corner-case bugs",
            "Implement functional coverage and code coverage; analyze coverage reports and drive coverage closure",
            "Perform RTL functional verification using simulation",
            "Execute regression tests and analyze simulation results",
            "Debug simulation failures, waveform issues, assertion failures, and scoreboard mismatches",
            "Collaborate with RTL designers to identify and resolve functional bugs",
            "Document verification results and maintain verification reports",
        ],
        "skills": [
            "SystemVerilog", "Verilog", "Universal Verification Methodology (UVM)",
            "Object-Oriented Programming (OOP)", "Constrained Random Verification (CRV)",
            "Randomization and Constraints", "Assertion-Based Verification (ABV)",
            "Coverage-Driven Verification (CDV)", "Functional Coverage", "Code Coverage",
            "Cross Coverage", "SystemVerilog Assertions (SVA)", "Linux",
        ],
        "tools": ["Synopsys VCS", "Synopsys Verdi", "Cadence Xcelium", "Siemens QuestaSim"],
    },
    "analog_layout": {
        "role": "Analog Layout Engineer",
        "responsibilities": [
            "Translate schematics into physical layout for analog/mixed-signal circuits, optimizing for performance and area",
            "Precisely place and route transistors, capacitors and resistors per foundry design rules",
            "Lay out key analog blocks — amplifiers, comparators, bias circuits, bandgaps, ADCs/DACs — with proper symmetry and shielding",
            "Maintain device matching, minimize parasitics, and apply noise-reduction techniques",
            "Run physical verification: DRC, LVS, ERC and antenna checks",
            "Handle ESD, latch-up and DFM (design-for-manufacturability) requirements in the layout",
            "Layout across technology generations from planar CMOS to FinFET, including memory layout",
        ],
        "skills": [
            "device matching (common-centroid, interdigitation, dummies, symmetry)",
            "parasitics and coupling management", "shielding and noise reduction",
            "DRC, LVS, ERC and antenna signoff", "ESD, latch-up and DFM",
            "advanced-node effects: WPE, STI, LOD, CMP", "FinFET and memory layout",
            "Perl/Shell scripting",
        ],
        "tools": ["Cadence Virtuoso XL / Layout Suite", "Cadence Custom Compiler",
                  "Mentor Calibre", "Synopsys Hercules", "StarRC/QRC", "SPICE"],
    },
}


def render_job_description(domain: str) -> str:
    """Render a domain's JD as a compact text block for the interview prompt.
    Returns "" for an unknown domain so the caller can fall back gracefully."""
    jd = JOB_DESCRIPTIONS.get(domain)
    if not jd:
        return ""
    resp = "\n".join(f"- {r}" for r in jd["responsibilities"])
    skills = ", ".join(jd["skills"])
    tools = ", ".join(jd["tools"])
    return (
        f"Role: {jd['role']}\n"
        f"Responsibilities:\n{resp}\n"
        f"Required skills: {skills}\n"
        f"Tools: {tools}"
    )
