"""
config.py — single source of truth for env vars, AI clients, and domain constants.

To add a new AI provider: add client init here. Every module imports from here.
To change domain topics: edit DOMAIN_TOPICS / DOMAIN_RESOURCES.
To add a new difficulty level: extend DIFFICULTY_LABELS and update matrices in agent/evaluator.py.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Environment ───────────────────────────────────────────────────────────────
ENV            = os.getenv("ENV", "development")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8001").split(",")

# ── AI keys ───────────────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
CEREBRAS_API_KEY  = os.getenv("CEREBRAS_API_KEY", "")

# ── Auth ──────────────────────────────────────────────────────────────────────
import secrets
JWT_SECRET     = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGO       = "HS256"
JWT_EXPIRE_MIN = 480

ADMIN_USER    = os.getenv("ADMIN_USER",    "admin")
ADMIN_PASS    = os.getenv("ADMIN_PASS",    "changeme_before_deploy")
REVIEWER_USER = os.getenv("REVIEWER_USER", "reviewer")
REVIEWER_PASS = os.getenv("REVIEWER_PASS", "changeme_before_deploy")

# ── AI Clients ────────────────────────────────────────────────────────────────
from openai import OpenAI

openai_client = OpenAI(api_key=OPENAI_API_KEY)

cerebras_client = None
if CEREBRAS_API_KEY:
    cerebras_client = OpenAI(api_key=CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1")
    print("Cerebras LLM ready.")

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
xai_client = None
if XAI_API_KEY:
    xai_client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
    print("xAI Grok ready.")

# ── TTS toggle ────────────────────────────────────────────────────────────────
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").lower() == "true"

# ── Parakeet AI Trap ─────────────────────────────────────────────────────────

# ── AI content detection ──────────────────────────────────────────────────────
SAPLING_API_KEY = os.getenv("SAPLING_API_KEY", "")
if SAPLING_API_KEY:
    print("Sapling AI content detector ready.")


# ── Level profiling — maps years of experience to calibrated expectations ─────
def get_level_profile(years: float) -> dict:
    """
    Returns a calibrated level profile based on years of experience.
    Used by eval prompt to set correct scoring expectations.
    """
    if years <= 0:
        return {
            "bucket": "fresh_graduate",
            "expectation": "Conceptual understanding only. No real tool exposure expected. "
                           "Should know definitions, purpose, and basic theory. "
                           "Cannot be expected to debug or use numbers.",
        }
    elif years <= 1:
        return {
            "bucket": "trained_fresher",
            "expectation": "Conceptual understanding + basic tool awareness from training programs. "
                           "Should have seen tools in a lab setting. Can be expected to know "
                           "standard workflows at a high level but not debug real issues.",
        }
    elif years <= 3:
        return {
            "bucket": "experienced_junior",
            "expectation": "1-3 years hands-on. Should know standard workflows, have run common "
                           "EDA flows, and be able to walk through scenarios. Expected to know "
                           "typical values and debug simple issues.",
        }
    else:
        return {
            "bucket": "experienced_senior",
            "expectation": "3+ years hands-on. Should know edge cases, tool-level detail, "
                           "numerical targets, trade-offs. Expected to debug complex issues "
                           "and explain why, not just what.",
        }


# ── Interview constants ───────────────────────────────────────────────────────
DIFFICULTY_LABELS = ["foundational", "basic", "intermediate", "advanced", "expert"]
FILLER_WORDS      = {"um","uh","like","basically","actually","so","right","okay","hmm","err"}
EXPECTED_PAUSE_BY_DIFFICULTY = {
    "foundational": 2.0, "basic": 3.0, "intermediate": 5.0, "advanced": 8.0, "expert": 10.0
}

# ── Domain knowledge ──────────────────────────────────────────────────────────
# To add a new domain: add an entry to all three dicts below.
DOMAIN_TOPICS = {
    "analog_layout": [
        "basic layout concepts","device matching","parasitic awareness",
        "latch-up and ESD","guard rings","DRC/LVS","symmetry techniques",
        "shielding and routing","technology awareness",
    ],
    "physical_design": [
        "floorplanning","power planning","placement","clock tree synthesis",
        "routing","timing closure","static timing analysis","DRC/LVS signoff","tool knowledge",
    ],
    "design_verification": [
        "verification methodologies","testbench architecture","functional coverage",
        "assertions and SVA","simulation vs formal","debugging skills",
        "protocol knowledge","regression and signoff","UVM",
    ],
}

DOMAIN_RESOURCES = {
    "analog_layout": [
        "Weste & Harris — CMOS VLSI Design, Chapter 3 (Layout)",
        "Razavi — Design of Analog CMOS Integrated Circuits, Chapter 18",
        "Virtuoso Layout Suite User Guide — Cadence documentation",
        "Calibre DRC/LVS User Manual — Mentor Graphics",
    ],
    "physical_design": [
        "Synopsys ICC2 User Guide — Floorplanning and Placement chapters",
        "Rabaey — Digital Integrated Circuits, Chapter 7",
        "Static Timing Analysis for Nanometer Designs — Jayaram Bhasker",
        "PrimeTime User Guide — Synopsys documentation",
    ],
    "design_verification": [
        "Spear & Tumbush — SystemVerilog for Verification, Chapters 4-7",
        "Bergeron — Writing Testbenches Using SystemVerilog",
        "UVM Cookbook — Mentor Verification Academy",
        "VCS Simulation User Guide — Synopsys documentation",
    ],
}

CONTRADICTION_PAIRS = {
    "analog_layout": [
        {"topic":"device matching",
         "angle_1":"What techniques do you use to achieve good device matching in layout?",
         "angle_2":"If two devices have identical layout but different orientations relative to the gradient, will they match? Why or why not?"},
        {"topic":"parasitic awareness",
         "angle_1":"How do you minimize parasitic capacitance in a layout?",
         "angle_2":"In a situation where two nets run parallel for 100 microns, what is the dominant parasitic concern and how would you quantify it?"},
        {"topic":"latch-up and ESD",
         "angle_1":"What causes latch-up in CMOS layout and how do you prevent it?",
         "angle_2":"If you have a circuit where the substrate contact spacing is 50 microns from the nearest NMOS, is that acceptable? Walk me through your reasoning."},
    ],
    "physical_design": [
        {"topic":"clock tree synthesis",
         "angle_1":"What is clock tree synthesis and what problem does it solve?",
         "angle_2":"If after CTS you have 200ps of skew on one branch, what are the first three things you would check and in what order?"},
        {"topic":"timing closure",
         "angle_1":"Explain the difference between setup and hold violations.",
         "angle_2":"You have a hold violation of 50ps on a path that passes through three buffers. Adding more buffers made it worse. What is happening and what is the correct fix?"},
        {"topic":"floorplanning",
         "angle_1":"What are the key objectives of floorplanning in physical design?",
         "angle_2":"You are floorplanning a block with 60% utilization and your timing is already tight. Where exactly would you place the critical path macros and why?"},
    ],
    "design_verification": [
        {"topic":"functional coverage",
         "angle_1":"What is functional coverage and why is it important in verification?",
         "angle_2":"Your regression shows 98% functional coverage but you still found a bug in silicon. How is that possible and what does it tell you about your coverage model?"},
        {"topic":"assertions and SVA",
         "angle_1":"What is the difference between a concurrent and immediate assertion in SVA?",
         "angle_2":"You have an assertion that never fires during simulation. Is that good or bad? How do you determine which it is?"},
        {"topic":"simulation vs formal",
         "angle_1":"What are the advantages of formal verification over simulation?",
         "angle_2":"Formal verification proved your design is correct but you still found a bug. What are three possible explanations?"},
    ],
}