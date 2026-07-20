"""
Deterministic lab catalog + learning path over the TC-*.html taxonomy.

Everything here is derived from the filename facets already in the index
(TC-<STAGE>-<PROVIDER>-<VARIANT>-<TYPE>-<NUM>.html) plus a small static map of
human labels/order. No LLM is involved, so counts, groupings and the learning
path are exact and never hallucinated.

A "lab" = one topic (Guided + its Challenge(s), with an optional Overview). Its
difficulty ladder is read from how many Challenge files the topic actually has:
    0 challenges -> Normal only            (e.g. PV labs)
    1 challenge  -> Normal -> Difficult
    2 challenges -> Normal -> Medium -> Difficult   (a few Synthesis topics)
"""

import re
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Static taxonomy: order, human labels, tools. Ordered by the RTL->GDSII flow.
# ---------------------------------------------------------------------------
STAGE_ORDER = ["SYN", "PNR", "STA", "LEC", "PV"]
STAGE_LABELS = {
    "SYN": "Synthesis",
    "PNR": "Place & Route",
    "STA": "Static Timing Analysis",
    "LEC": "Logic Equivalence Check",
    "PV": "Physical Verification",
}
PROVIDER_LABELS = {"SNPS": "Synopsys", "CDN": "Cadence", "SIE": "Siemens"}

DEFAULT_TRACK = "SNPS"   # single-tool learning track

# Per-stage variant order (the sequence a learner should follow within a stage).
VARIANT_ORDER = {
    "SYN": ["URR", "LS", "CT", "PS"],
    "PNR": ["DI", "FP", "PL", "CTS", "RT", "CF"],
    "STA": ["STA", "DMSA"],
    "LEC": ["R2N", "CG", "N2N"],
    "PV": ["DRC", "LVS", "ANT", "DF_FEOL", "DF_BEOL", "GDS"],
}

# Human names for each (stage, variant), taken from the lab titles.
VARIANT_LABELS = {
    ("SYN", "URR"): "Synthesis & Linking (Unresolved References)",
    ("SYN", "LS"): "Logic-Aware Synthesis",
    ("SYN", "CT"): "Synthesis Check Timing",
    ("SYN", "PS"): "Physical-Aware Synthesis",
    ("PNR", "DI"): "Design Initialization",
    ("PNR", "FP"): "Floorplanning",
    ("PNR", "PL"): "Placement",
    ("PNR", "CTS"): "Clock Tree Synthesis",
    ("PNR", "RT"): "Routing",
    ("PNR", "CF"): "Chip Finishing (Tape-Out Output)",
    ("STA", "STA"): "Static Timing Analysis",
    ("STA", "DMSA"): "Distributed Multi-Scenario Analysis (Signoff ECO)",
    ("LEC", "R2N"): "RTL-to-Gate LEC",
    ("LEC", "CG"): "RTL-to-Gate LEC with Clock Gating",
    ("LEC", "N2N"): "Gate-to-Gate LEC",
    ("PV", "DRC"): "Design Rule Check (DRC)",
    ("PV", "LVS"): "CDL Generation & LVS",
    ("PV", "ANT"): "Antenna Check",
    ("PV", "DF_FEOL"): "FEOL Dummy Fill",
    ("PV", "DF_BEOL"): "BEOL Dummy Fill",
    ("PV", "GDS"): "Merge Fill GDS (Stream-Out)",
}

# The tool used for each (stage, provider).
TOOL = {
    ("SYN", "SNPS"): "Synopsys Design Compiler",
    ("SYN", "CDN"): "Cadence Genus",
    ("PNR", "SNPS"): "Synopsys IC Compiler II (ICC2)",
    ("PNR", "CDN"): "Cadence Innovus",
    ("STA", "SNPS"): "Synopsys PrimeTime",
    ("STA", "CDN"): "Cadence Tempus",
    ("LEC", "SNPS"): "Synopsys Formality",
    ("PV", "SIE"): "Siemens Calibre",
}

# Excluded from the catalog entirely (mislabeled duplicate; see zoho_sync.py).
_EXCLUDE = {("LEC", "CDN", "R2N")}

_SRC_RE = re.compile(
    r"^TC-(?P<stage>[A-Z]+)-(?P<prov>[A-Z]+)-(?P<var>.+)-"
    r"(?P<dt>GD|CH|OV)-(?P<num>\d+)\.html$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Catalog construction
# ---------------------------------------------------------------------------
def _variant_label(stage, variant):
    return VARIANT_LABELS.get((stage, variant), variant)


def _ordered_variants(stage, present):
    """Variants of a stage in learning order, then any not in the map."""
    ordered = [v for v in VARIANT_ORDER.get(stage, []) if v in present]
    extra = sorted(v for v in present if v not in ordered)
    return ordered + extra


def build_catalog(engine):
    """Return {stage: {provider: {variant: {ch, has_gd, has_ov}}}} from the index."""
    topics = {}
    for src in {c["source"] for c in engine.chunks}:
        m = _SRC_RE.match(src)
        if not m:
            continue
        stage = m.group("stage").upper()
        prov = m.group("prov").upper()
        var = m.group("var").upper()
        dt = m.group("dt").upper()
        if (stage, prov, var) in _EXCLUDE:
            continue
        t = topics.setdefault((stage, prov, var),
                              {"ch": 0, "has_gd": False, "has_ov": False})
        if dt == "CH":
            t["ch"] += 1
        elif dt == "GD":
            t["has_gd"] = True
        elif dt == "OV":
            t["has_ov"] = True

    # Reshape into ordered stage -> provider -> variant.
    cat = OrderedDict()
    for stage in STAGE_ORDER:
        provs = OrderedDict()
        for (st, pr, var), t in topics.items():
            if st != stage:
                continue
            provs.setdefault(pr, {})[var] = t
        if provs:
            cat[stage] = provs
    # Any stage not in STAGE_ORDER (defensive).
    for (st, pr, var), t in topics.items():
        if st not in cat and st not in STAGE_ORDER:
            cat.setdefault(st, OrderedDict()).setdefault(pr, {})[var] = t
    return cat


def difficulty_tiers(ch_count):
    """Human difficulty ladder for a topic given its challenge count."""
    if ch_count <= 0:
        return [("Normal", "Guided")]
    if ch_count == 1:
        return [("Normal", "Guided"), ("Difficult", "Challenge")]
    labels = [("Normal", "Guided"), ("Medium", "Challenge 1"),
              ("Difficult", "Challenge 2")]
    return labels[:ch_count + 1]


def _tier_summary(ch_count):
    return " -> ".join(lvl for lvl, _ in difficulty_tiers(ch_count))


def total_labs(cat):
    return sum(len(vs) for provs in cat.values() for vs in provs.values())


# ---------------------------------------------------------------------------
# Rendering (markdown for the chat UI)
# ---------------------------------------------------------------------------
def inventory_md(cat):
    n = total_labs(cat)
    lines = [f"We have **{n} labs** across **{len(cat)} stages**. "
             "Each lab is a topic with a Guided (Normal) walkthrough and, where "
             "available, Challenge exercises that raise the difficulty.\n"]
    for stage, provs in cat.items():
        cnt = sum(len(vs) for vs in provs.values())
        tools = ", ".join(sorted({TOOL.get((stage, p), PROVIDER_LABELS.get(p, p))
                                  for p in provs}))
        lines.append(f"### {STAGE_LABELS.get(stage, stage)} — {cnt} labs "
                     f"({tools})")
        for prov in provs:
            for var in _ordered_variants(stage, provs[prov]):
                t = provs[prov][var]
                lines.append(f"- {_variant_label(stage, var)} "
                             f"[{PROVIDER_LABELS.get(prov, prov)}] "
                             f"· {_tier_summary(t['ch'])}")
        lines.append("")
    return "\n".join(lines).strip()


def stage_md(cat, stage):
    provs = cat.get(stage)
    if not provs:
        return f"No labs are indexed for {STAGE_LABELS.get(stage, stage)}."
    cnt = sum(len(vs) for vs in provs.values())
    lines = [f"**{STAGE_LABELS.get(stage, stage)}** has **{cnt} labs**:\n"]
    for prov in provs:
        tool = TOOL.get((stage, prov), PROVIDER_LABELS.get(prov, prov))
        lines.append(f"### {tool}")
        for var in _ordered_variants(stage, provs[prov]):
            t = provs[prov][var]
            lines.append(f"- **{_variant_label(stage, var)}** — "
                         f"{_tier_summary(t['ch'])}")
        lines.append("")
    return "\n".join(lines).strip()


def _track_provider(cat, stage, track):
    """Provider to use for a stage on a given track: the track if present,
    otherwise the stage's only/first available provider (e.g. PV -> Siemens)."""
    provs = cat.get(stage, {})
    if track in provs:
        return track
    return next(iter(provs), None)


def learning_path_md(cat, track=DEFAULT_TRACK, start_stage=None):
    """Ordered curriculum on a single tool track.

    start_stage: if given, the path begins at that stage (skips earlier ones).
    """
    track_label = PROVIDER_LABELS.get(track, track)
    stages = list(cat.keys())
    if start_stage and start_stage in stages:
        stages = stages[stages.index(start_stage):]

    lines = [f"Here's a suggested **learning path** ({track_label} track, "
             "switching tools only where a stage requires it). Follow the flow "
             "order; inside each lab go Normal → then the Challenge tiers:\n"]
    step = 1
    for stage in stages:
        prov = _track_provider(cat, stage, track)
        if not prov:
            continue
        tool = TOOL.get((stage, prov), PROVIDER_LABELS.get(prov, prov))
        vs = cat[stage][prov]
        lines.append(f"### {step}. {STAGE_LABELS.get(stage, stage)} — {tool}")
        for var in _ordered_variants(stage, vs):
            t = vs[var]
            lines.append(f"   - {_variant_label(stage, var)} "
                         f"· {_tier_summary(t['ch'])}")
        lines.append("")
        step += 1
    lines.append("_Tip: say \"I've finished synthesis\" (or any stage) and I'll "
                 "start the path from the next one._")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Intent detection — conservative, so normal Q&A is never hijacked.
# ---------------------------------------------------------------------------
_STAGE_SYNONYMS = [
    ("SYN", r"synthesis|\bsyn\b|genus|design\s+compiler|\bdc\s*shell\b"),
    ("PNR", r"\bpnr\b|place\s*(and|&)?\s*route|\bp&r\b|physical\s+design|"
            r"innovus|icc2|ic\s+compiler|floorplan|placement|routing"),
    ("STA", r"\bsta\b|static\s+timing|timing\s+analysis|primetime|tempus|"
            r"signoff\s+timing"),
    ("LEC", r"\blec\b|logic\s+equivalence|equivalence\s+check|formality"),
    ("PV", r"\bpv\b|physical\s+verification|\bdrc\b|\blvs\b|calibre"),
]

_PATH_RE = re.compile(
    r"learning\s*(path|flow|plan|journey)|road\s*map|study\s*plan|curriculum|"
    r"where\s+(do|should|can)\s+i\s+(start|begin)|where\s+to\s+(start|begin)|"
    r"which\s+lab.*\bfirst\b|order\s+of\s+(the\s+)?labs|sequence\s+of\s+labs|"
    r"how\s+(do|should)\s+i\s+(start|learn|proceed|study)|start\s+learning|"
    r"where\s+(to\s+go\s+)?next|what('?s)?\s+next|next\s+(lab|step|stage|topic)|"
    r"continue\s+(learning|studying|my)",
    re.IGNORECASE,
)

_INVENTORY_RE = re.compile(
    r"how\s+many\s+(labs|topics|modules)|"
    r"what\s+(labs|topics)\s+(do\s+we|are|does)|"
    r"list\s+(all\s+)?(the\s+)?labs|all\s+(the\s+)?labs\b|total\s+(number\s+of\s+)?labs|"
    r"what\s+are\s+(all\s+)?(the|those)\s+labs|labs\s+do\s+we\s+have|"
    r"what\s+do\s+we\s+have|catalog(ue)?\s+of\s+labs|what('?s| is)\s+available",
    re.IGNORECASE,
)

_LAB_LISTING_RE = re.compile(
    r"\blabs?\b|\btopics?\b|\btestcases?\b|list|show|all\b|what\s+are|which",
    re.IGNORECASE,
)

_FINISHED_RE = re.compile(
    r"\b(finish|finished|complete|completed|done|did|know|learnt|learned|"
    r"already\s+did)\b", re.IGNORECASE,
)


def _find_stage(q):
    for code, pat in _STAGE_SYNONYMS:
        if re.search(pat, q, re.IGNORECASE):
            return code
    return None


def _find_all_stages(q):
    """All stages mentioned in the question (used to find the furthest done)."""
    return [code for code, pat in _STAGE_SYNONYMS
            if re.search(pat, q, re.IGNORECASE)]


def _find_track(q):
    if re.search(r"cadence|genus|innovus|tempus|conformal", q, re.IGNORECASE):
        return "CDN"
    if re.search(r"synopsys|design\s+compiler|icc2|primetime|formality|\bdc\b",
                 q, re.IGNORECASE):
        return "SNPS"
    return None


def detect_intent(question):
    """Classify a catalog-style question. Returns a dict or None.

    Kinds:
      {"kind": "path", "track": str|None, "start_stage": str|None}
      {"kind": "inventory"}
      {"kind": "stage", "stage": str}
    """
    q = question or ""
    # 1. Learning path (also handles "I've finished X (and Y), where next?").
    if _PATH_RE.search(q):
        start = None
        done = _find_all_stages(q)
        if done and _FINISHED_RE.search(q):
            # Begin AFTER the furthest stage they've already completed, so
            # "finished synthesis and pnr" starts at STA, not PNR.
            furthest = max(done, key=lambda c: STAGE_ORDER.index(c)
                           if c in STAGE_ORDER else -1)
            i = STAGE_ORDER.index(furthest) if furthest in STAGE_ORDER else -1
            if 0 <= i < len(STAGE_ORDER) - 1:
                start = STAGE_ORDER[i + 1]
        return {"kind": "path", "track": _find_track(q), "start_stage": start}

    # 2. A specific stage's labs ("show all synthesis labs").
    stage = _find_stage(q)
    if stage and _LAB_LISTING_RE.search(q):
        return {"kind": "stage", "stage": stage}

    # 3. Whole-corpus inventory.
    if _INVENTORY_RE.search(q):
        return {"kind": "inventory"}
    return None


def answer(question, engine, default_track=DEFAULT_TRACK):
    """Return a markdown answer for a catalog intent, or None if not catalog."""
    intent = detect_intent(question)
    if not intent:
        return None
    cat = build_catalog(engine)
    if intent["kind"] == "inventory":
        return inventory_md(cat)
    if intent["kind"] == "stage":
        return stage_md(cat, intent["stage"])
    if intent["kind"] == "path":
        return learning_path_md(cat, track=intent.get("track") or default_track,
                                start_stage=intent.get("start_stage"))
    return None
