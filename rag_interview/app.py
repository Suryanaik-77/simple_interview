"""
Standalone RAG application over the lab-testcase HTML content.

A small FastAPI app: the user asks a question, the engine retrieves the most
relevant lab sections, and an LLM answers grounded in those sections (with
citations). Completely independent of the main interview platform.

Run:
    cd rag_interview
    uvicorn app:app --reload --port 8100
Then open http://localhost:8100
"""

import os
import re
import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel
from openai import OpenAI
from datetime import datetime

from rag_engine import RAGEngine, sanitize_paths, parse_facets
from dv_rag_engine import DVRAGEngine
from analog_rag_engine import AnalogRAGEngine
from platform_rag_engine import PlatformRAGEngine

_HERE = os.path.dirname(os.path.abspath(__file__))

# Chat/verify run on DeepSeek V4 Flash (OpenAI-compatible API). Embeddings stay
# on OpenAI inside rag_engine — DeepSeek has no embeddings endpoint.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CHAT_MODEL = "deepseek-v4-flash"     # final answer
VERIFY_MODEL = "deepseek-v4-flash"   # small verifier agent
RETRIEVE_K = 6                       # retrieve 6 chunks, evaluate progressively (3+3)
BATCH_SIZE = 3                       # evaluate 3 chunks at a time for token optimization
# Only offer a clarify when retrieval is actually confident. Legit lab questions
# score ~0.55-0.65 cosine; off-topic/nonsense scores <0.2. Below this we skip
# clarify and let verify/answer say the content isn't present.
MIN_CLARIFY_SCORE = 0.40

# Token pricing (USD per 1M tokens)
PRICING = {
    "openai_embed": {"input": 0.13},  # text-embedding-3-large
    "deepseek_v4_flash": {"input": 0.014, "output": 0.14, "cache_hit": 0.0014},  # reasoning model
}

# Token tracking storage (in-memory for now, can be moved to DB)
_token_stats = []

# Question history - keeps last 10 questions per session
from collections import deque
_question_history = deque(maxlen=10)


def _get_source_name(hit, domain):
    """Get the appropriate source name field based on domain."""
    if domain == "pd":
        return hit.get("lab_name", "Unknown")
    elif domain == "dv":
        return hit.get("module_name", hit.get("lab_name", "Unknown"))
    elif domain == "analog":
        return hit.get("topic_name", hit.get("lab_name", "Unknown"))
    return hit.get("lab_name", "Unknown")


class TokenTracker:
    """Track token usage and costs for a single query."""

    def __init__(self, question: str):
        self.question = question
        self.started_at = datetime.utcnow()
        self.embed_tokens = 0
        self.verify_batches = []  # List of {input, output, cache_hit, batch_num}
        self.answer_tokens = {"input": 0, "output": 0, "cache_hit": 0}
        self.clarify_tokens = {"input": 0, "output": 0, "cache_hit": 0}

    def track_embedding(self, usage):
        """Track OpenAI embedding tokens."""
        self.embed_tokens = usage.total_tokens if hasattr(usage, 'total_tokens') else usage.get('total_tokens', 0)

    def track_verify(self, usage, batch_num):
        """Track verifier batch tokens."""
        self.verify_batches.append({
            "batch_num": batch_num,
            "input": usage.prompt_tokens,
            "output": usage.completion_tokens,
            "cache_hit": getattr(usage, 'prompt_tokens_details', {}).get('cached_tokens', 0) if hasattr(usage, 'prompt_tokens_details') else 0
        })

    def track_answer(self, usage):
        """Track answer generation tokens."""
        cache_hit = 0
        if hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
            cache_hit = getattr(usage.prompt_tokens_details, 'cached_tokens', 0)

        self.answer_tokens = {
            "input": usage.prompt_tokens,
            "output": usage.completion_tokens,
            "cache_hit": cache_hit
        }

    def track_clarify(self, usage):
        """Track clarify decision tokens."""
        cache_hit = 0
        if hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
            cache_hit = getattr(usage.prompt_tokens_details, 'cached_tokens', 0)

        self.clarify_tokens = {
            "input": usage.prompt_tokens,
            "output": usage.completion_tokens,
            "cache_hit": cache_hit
        }

    def calculate_cost(self):
        """Calculate total cost in USD."""
        cost = 0.0

        # Embedding cost
        cost += (self.embed_tokens / 1_000_000) * PRICING["openai_embed"]["input"]

        # Verifier batches cost
        for batch in self.verify_batches:
            cost += (batch["input"] / 1_000_000) * PRICING["deepseek_v4_flash"]["input"]
            cost += (batch["output"] / 1_000_000) * PRICING["deepseek_v4_flash"]["output"]
            cost += (batch["cache_hit"] / 1_000_000) * PRICING["deepseek_v4_flash"]["cache_hit"]

        # Answer cost
        cost += (self.answer_tokens["input"] / 1_000_000) * PRICING["deepseek_v4_flash"]["input"]
        cost += (self.answer_tokens["output"] / 1_000_000) * PRICING["deepseek_v4_flash"]["output"]
        cost += (self.answer_tokens["cache_hit"] / 1_000_000) * PRICING["deepseek_v4_flash"]["cache_hit"]

        # Clarify cost
        cost += (self.clarify_tokens["input"] / 1_000_000) * PRICING["deepseek_v4_flash"]["input"]
        cost += (self.clarify_tokens["output"] / 1_000_000) * PRICING["deepseek_v4_flash"]["output"]
        cost += (self.clarify_tokens["cache_hit"] / 1_000_000) * PRICING["deepseek_v4_flash"]["cache_hit"]

        return cost

    def get_summary(self):
        """Get token usage summary."""
        total_verify_input = sum(b["input"] for b in self.verify_batches)
        total_verify_output = sum(b["output"] for b in self.verify_batches)
        total_verify_cache = sum(b["cache_hit"] for b in self.verify_batches)

        total_input = total_verify_input + self.answer_tokens["input"] + self.clarify_tokens["input"]
        total_output = total_verify_output + self.answer_tokens["output"] + self.clarify_tokens["output"]
        total_cache = total_verify_cache + self.answer_tokens["cache_hit"] + self.clarify_tokens["cache_hit"]

        return {
            "question": self.question[:100],
            "timestamp": self.started_at.isoformat(),
            "tokens": {
                "embedding": self.embed_tokens,
                "verify_batches": len(self.verify_batches),
                "verify_input": total_verify_input,
                "verify_output": total_verify_output,
                "verify_cache_hit": total_verify_cache,
                "answer_input": self.answer_tokens["input"],
                "answer_output": self.answer_tokens["output"],
                "answer_cache_hit": self.answer_tokens["cache_hit"],
                "clarify_input": self.clarify_tokens["input"],
                "clarify_output": self.clarify_tokens["output"],
                "total_input": total_input,
                "total_output": total_output,
                "total_cache_hit": total_cache,
                "total": self.embed_tokens + total_input + total_output,
            },
            "cost_usd": self.calculate_cost(),
            "batches_detail": self.verify_batches
        }

    def save(self):
        """Save to global stats."""
        _token_stats.append(self.get_summary())

app = FastAPI(title="Lab RAG")
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

# Build/load all four RAG engines at startup
pd_engine = RAGEngine.load_or_build()          # Physical Design RAG
dv_engine = DVRAGEngine.load_or_build()        # Design Verification RAG
analog_engine = AnalogRAGEngine.load_or_build()  # Analog Layout RAG
platform_engine = PlatformRAGEngine()          # Platform FAQ RAG

# Map for easy access
RAG_ENGINES = {
    "pd": pd_engine,
    "dv": dv_engine,
    "analog": analog_engine
}

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=DEEPSEEK_BASE_URL,
    timeout=60.0,
    max_retries=2,
)

SYSTEM_PROMPT = (
    "You are a precise VLSI engineer assisting with lab testcases. Follow these "
    "rules:\n"
    "1. Prefer the provided context sections — use them whenever they answer "
    "the question, citing facts as [1], [2] matching the numbered context "
    "blocks. If the sections do NOT contain the answer AND the question is a "
    "general VLSI/EDA/semiconductor question (not tied to a specific named "
    "lab), answer it from your own general VLSI/EDA knowledge instead of "
    "refusing, opening with 'Not covered in the lab material, but generally:' "
    "so it is clear the answer did not come from the indexed content. If the "
    "question is NOT about VLSI/EDA/semiconductors at all, decline and say "
    "you only answer VLSI-related questions.\n"
    "2. If the user names a specific lab/testcase and asks what THAT lab/"
    "testcase does or contains, only use sections from that same lab — do not "
    "invent lab-specific facts (a file, a step, a value) from general "
    "knowledge, even under rule 1. If none of the context is from the lab "
    "they asked about, reply that the information is not available for that "
    "lab.\n"
    "3. Do NOT accept a false premise about a SPECIFIC lab/testcase (e.g. a "
    "file or step that isn't there) — say it is not present rather than "
    "describing it from general knowledge. This rule covers claims tied to a "
    "named lab, not general VLSI concepts (rule 1 covers those).\n"
    "4. Be concise and technical. (A separate step already asks the user to "
    "disambiguate when needed, so assume the question is as specific as given "
    "and answer it.)"
)

# When a question could apply to several tools/flows/testcases, a good engineer
# asks which one the user means instead of guessing. This agent decides that and
# returns concrete options the UI renders as clickable buttons.
CLARIFY_PROMPT = (
    "You are a VLSI physical-design assistant deciding whether a user's question "
    "genuinely needs the user to disambiguate before it can be answered well, "
    "given the lab sections retrieved for it.\n"
    "You are told, per FACET, the distinct values the retrieved sections span:\n"
    "  - provider: the EDA tool vendor (Synopsys, Cadence, Siemens/Calibre).\n"
    "  - stage: the flow stage (Synthesis, LEC, PnR, PV, STA).\n"
    "Decide with an engineer's judgment:\n"
    "  - Use your OWN knowledge to dismiss a FALSE spread. If the question is "
    "about CTS, floorplan, placement, routing or chip finish, the stage is "
    "obviously PnR even if a stray Synthesis section was retrieved — do NOT ask "
    "about stage. DRC/LVS/antenna/fill are PV; equivalence is LEC; etc.\n"
    "  - If the user already named the tool or stage — including via a tool name "
    "(ICC2/Design Compiler/PrimeTime/Formality = Synopsys; Innovus/Genus/Tempus/"
    "Conformal = Cadence; Calibre = Siemens) — that facet is settled; do NOT ask "
    "it.\n"
    "  - If the question asks to LIST or COUNT labs, explain a concept, or is "
    "about a whole stage or the whole course, it is NOT ambiguous — answer it; do "
    "NOT ask.\n"
    "  - Only ask when picking one value would genuinely change the answer (e.g. "
    "the exact command/flow differs between Synopsys and Cadence for the SAME "
    "task the user asked about).\n"
    "Return STRICT JSON: {\"clarify\": bool, \"facet\": \"provider\"|\"stage\"|"
    "null, \"question\": string, \"options\": [labels]}. When clarify=true, set "
    "facet to the ONE facet to disambiguate and options to the human labels for "
    "its values EXACTLY as given to you. When clarify=false, question=\"\" and "
    "options=[]."
)

# Whether to ask the student "Synopsys or Cadence?" before listing a stage's
# labs is split into a FACT and an INTENT part. The fact — which stages offer a
# real tool choice — is read deterministically from the corpus (a stage with
# 2+ providers present). The intent — is the user actually asking for a single
# stage's ordered labs (vs. the overall flow, or where to begin) — is the one
# fuzzy bit, judged by a narrow LLM yes/no. (An earlier open-ended "does this
# need a tool?" prompt wobbled: the model kept reasoning "the modules are the
# same in both tools, so no" — true, but it defeats the point of asking.)
STAGE_INTENT_PROMPT = (
    "A student is browsing a physical-design course whose flow has stages "
    "(Synthesis, LEC, PnR, PV, STA). Decide whether their question asks for the "
    "concrete labs or the ordered module path WITHIN one specific stage — the "
    "labs they would actually run — as opposed to a general question about the "
    "overall flow, the list of stages, where to begin the course, or why the "
    "order is what it is.\n"
    "Return STRICT JSON: {\"labs_of_stage\": bool}. Example true: 'give me the "
    "synthesis stage labs in order'. Example false: 'if I already know "
    "synthesis, where should I start?' (that asks about the entry point)."
)

# The corpus is already path-sanitized at ingest (rag_engine.sanitize_paths),
# so the index is clean. We still sanitize LLM outputs as defense-in-depth in
# case a model ever echoes a central path from its own priors.
_sanitize_paths = sanitize_paths


VERIFY_PROMPT = (
    "You select which retrieved sections are actually relevant to the user's "
    "question AND judge whether they are enough to answer it. Consider the "
    "specific lab/testcase the user names. Return STRICT JSON: "
    "{\"relevant\": [<indices>], \"sufficient\": <bool>, \"needs_more\": <bool>, "
    "\"navigate_hint\": <string>}.\n"
    "- relevant: include an index only if that section genuinely helps answer "
    "THIS question for the lab asked about. If the user names a lab and a "
    "section is from a different lab, exclude it. If nothing is relevant, "
    "return an empty list.\n"
    "- sufficient: true ONLY if the relevant sections above already contain "
    "everything needed to answer the question completely and specifically "
    "(exact command, value, file, or steps asked for). Set it false if they "
    "are only partially on-topic, look truncated, or hint at the answer "
    "without stating it — those need more chunks.\n"
    "- needs_more: true if relevant sections provide partial info but need "
    "more context from additional chunks. False if sufficient or no answer possible.\n"
    "- navigate_hint: When needs_more=true (partial answer) OR no relevant sections "
    "found, suggest specific labs/sections where complete info can be found. "
    "Format: 'For complete details, check [Lab Name] > [Section Name] or see [TC-XXX-GD-001] Guided lab'. "
    "Leave empty only if sufficient=true (complete answer found)."
)


class AskRequest(BaseModel):
    question: str
    domain: str = "pd"           # "pd", "dv", or "analog" - domain selection
    lab_name: str | None = None
    k: int = 5
    allow_clarify: bool = True   # False once the user has picked an option
    facets: dict | None = None   # {"provider": "SNPS"} once a facet is picked


def _verify_relevant(question, hits, batch_start=0, batch_size=BATCH_SIZE, tracker=None, batch_num=0, domain="pd"):
    """Small agent: pick indices of relevant hits and judge sufficiency.

    Progressive evaluation: checks chunks in batches (default 3 at a time).

    Args:
        question: User's question
        hits: Retrieved chunks
        batch_start: Starting index for this batch
        batch_size: Number of chunks to evaluate in this batch
        tracker: TokenTracker instance for tracking usage
        batch_num: Batch number for tracking
        domain: Domain type (pd/dv/analog) for correct field names

    Returns:
        (relevant_indices, sufficient, needs_more, navigate_hint)
        - relevant_indices: list of indices that are relevant
        - sufficient: True if these chunks fully answer the question
        - needs_more: True if partial answer, need next batch
        - navigate_hint: Suggestion where to look if no answer found
    """
    # Evaluate only the current batch of chunks
    batch_end = min(batch_start + batch_size, len(hits))
    batch_hits = hits[batch_start:batch_end]

    # Give the verifier the full sub-chunk (not a 220-char preview) so its
    # sufficiency judgement is made on the actual retrieved text.
    listing = "\n".join(
        f"{i}. [source: {_get_source_name(h, domain)} | section: {h['heading']}] "
        f"{_sanitize_paths(h['content'])}"
        for i, h in enumerate(batch_hits)
    )
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL,
            temperature=0,
            # v4-flash thinks first (reasoning_content), then emits the JSON in
            # content — budget enough tokens for both.
            max_tokens=1024,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": VERIFY_PROMPT},
                {"role": "user",
                 "content": f"Question: {question}\n\nSections:\n{listing}"},
            ],
        )

        # Track tokens
        if tracker and hasattr(resp, 'usage'):
            tracker.track_verify(resp.usage, batch_num)

        data = json.loads(resp.choices[0].message.content)
        # Map batch-local indices back to global indices
        batch_idxs = [int(i) for i in data.get("relevant", [])
                      if isinstance(i, (int, float)) and 0 <= int(i) < len(batch_hits)]
        idxs = [batch_start + i for i in batch_idxs]
        sufficient = bool(data.get("sufficient", False))
        needs_more = bool(data.get("needs_more", False))
        navigate_hint = data.get("navigate_hint", "")
        return idxs, sufficient, needs_more, navigate_hint
    except Exception:
        # On any failure, fall back to the current batch and mark as insufficient
        return list(range(batch_start, batch_end)), False, True, ""


# Facets whose value materially changes the answer. Each entry:
# (facet_key, label_key, default_question_text). doc_type (guided vs challenge)
# is deliberately NOT a clarify facet — the guided page reliably carries the
# commands, so we answer instead of asking.
_CLARIFY_FACETS = (
    ("stage", "stage_label", "Which flow/stage do you mean?"),
    ("provider", "provider_label", "Which tool do you mean?"),
)
_FACET_Q = {fk: q for fk, _lk, q in _CLARIFY_FACETS}


def _facet_spread(hits):
    """Deterministic DATA (not a heuristic): for each clarify facet, the distinct
    {code: label} the retrieved hits actually span. Only facets with 2+ distinct
    values are returned — those are the only ones there could be a choice about."""
    spread = {}
    for fk, lk, _q in _CLARIFY_FACETS:
        vals = {}
        for h in hits:
            code = (h.get("facets") or {}).get(fk)
            if code and code not in vals:
                vals[code] = (h.get("facets") or {}).get(lk) or code
        if len(vals) >= 2:
            spread[fk] = vals
    return spread


def _clarify_decision(question, hits):
    """LLM judgment: does this question genuinely need the user to disambiguate a
    facet before we can answer well? The facet SPREAD (what the hits actually
    span) is computed deterministically and handed to the model, which applies
    engineering judgment — dismissing false spreads it can resolve itself (CTS is
    obviously PnR), skipping facets the user already named, and never asking for
    list/count/concept questions. Returns a facet-shaped clarify dict (so the
    picked option still hard-restricts the corpus on the round-trip), or None."""
    spread = _facet_spread(hits)
    if not spread:
        return None                       # hits agree on every facet → no choice

    spread_desc = "\n".join(
        f"- {fk}: " + ", ".join(sorted(vals.values()))
        for fk, vals in spread.items())
    topics = "\n".join(
        f"  · [{(h.get('facets') or {}).get('stage_label', '?')}/"
        f"{(h.get('facets') or {}).get('provider_label', '?')}] "
        f"{h['lab_name']} :: {h['heading']}"
        for h in hits[:6])
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL, temperature=0, max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLARIFY_PROMPT},
                {"role": "user", "content":
                    f"Question: {question}\n\n"
                    f"Facets the retrieved sections span:\n{spread_desc}\n\n"
                    f"Retrieved section topics:\n{topics}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception:
        return None
    if not data.get("clarify"):
        return None
    fk = data.get("facet")
    if fk not in spread:                  # model picked a facet that isn't split
        return None
    vals = spread[fk]                     # {code: label}
    # Map the model's chosen labels back to REAL codes; drop anything invented.
    by_label = {str(l).lower(): c for c, l in vals.items()}
    codes = []
    for o in data.get("options", []):
        c = by_label.get(str(o).strip().lower())
        if c and c not in codes:
            codes.append(c)
    if len(codes) < 2:                    # fall back to all real values for facet
        codes = list(vals.keys())
    return {
        "question": data.get("question") or _FACET_Q[fk],
        "options": [vals[c] for c in codes],
        "facet": fk,
        "option_values": codes,
    }


_PROVIDER_LABEL = {"SNPS": "Synopsys", "CDN": "Cadence", "SIE": "Siemens"}

# Tool PRODUCT names -> owning provider. This is vendor fact (like SNPS=Synopsys),
# not an interview heuristic: each tool belongs to exactly one EDA vendor. Names
# taken from the ones that actually appear in the corpus. When a question names
# one of these (e.g. "ICC2 floorplanning"), the tool is already specified, so a
# "Synopsys or Cadence?" clarify is redundant — we resolve it silently instead.
_PROVIDER_TOOL_RE = {
    "SNPS": re.compile(
        r"\bicc2\b|\bic\s*compiler\b|\bfusion\s*compiler\b|\bdesign\s*compiler\b"
        r"|\bprimetime\b|\bformality\b|\bstarrc\b|\bvcs\b", re.I),
    "CDN": re.compile(
        r"\binnovus\b|\bgenus\b|\btempus\b|\bconformal\b|\bvoltus\b|\bquantus\b",
        re.I),
    "SIE": re.compile(r"\bcalibre\b", re.I),
}


def _provider_from_tool(question):
    """Return the provider code uniquely implied by a tool name in the question,
    or None if zero or more than one vendor's tools are named (ambiguous)."""
    hit = [p for p, rx in _PROVIDER_TOOL_RE.items() if rx.search(question)]
    return hit[0] if len(hit) == 1 else None

# Which stages a question can name, matched case-insensitively with word
# boundaries (so "sta" doesn't fire on "stage"/"start", "pv" not on "provide").
_STAGE_PATTERNS = {
    "SYN": re.compile(r"\bsynthesis\b|\bsynth\b", re.I),
    "PNR": re.compile(r"\bpnr\b|\bp\s*&\s*r\b|place\s*(?:and|&)\s*route", re.I),
    "STA": re.compile(r"\bsta\b|static\s+timing|timing\s+analysis", re.I),
    "LEC": re.compile(r"\blec\b|equivalence|formality|conformal", re.I),
    "PV": re.compile(r"\bpv\b|physical\s+verification", re.I),
}


def _asks_stage_labs(question):
    """Narrow LLM gate: is the user asking for a single stage's ordered labs?"""
    try:
        resp = _client.chat.completions.create(
            model=VERIFY_MODEL,
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": STAGE_INTENT_PROMPT},
                {"role": "user", "content": f"Question: {question}"},
            ],
        )
        return bool(json.loads(resp.choices[0].message.content).get("labs_of_stage"))
    except Exception:
        return False


def _stage_catalog(question):
    """If the question asks for ONE specific stage's ordered labs, return that
    stage code — else None. Deterministic stage match + narrow LLM intent gate.
    Such questions are catalog questions answered from the learning-path page,
    even when a specific lab happens to rank #1 in retrieval.
    """
    named = [st for st, pat in _STAGE_PATTERNS.items() if pat.search(question)]
    if len(named) != 1:                     # need exactly one stage named
        return None
    if not _asks_stage_labs(question):      # e.g. "where do I start" -> not this
        return None
    return named[0]


OVERVIEW_SOURCE = "pd_learning_path.html"


def _overview_doc_sections():
    """Every section of the learning-path page, in document order, fully
    reassembled. Catalog-level answers ground on the whole (tiny) page so they
    are complete — all five stages, each stage's modules in order — instead of
    a lossy top-k that can split a section at the chunk boundary and drop half
    the stages.
    """
    out, seen = [], set()
    for c in engine.chunks:
        if c["source"] != OVERVIEW_SOURCE:
            continue
        key = (c["lab_name"], c["heading"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "lab_name": c["lab_name"],
            "heading": c["heading"],
            "source": c["source"],
            "score": 1.0,
            "facets": c.get("facets") or {},
            "content": _sanitize_paths(
                engine.get_section(c["lab_name"], c["heading"])),
        })
    return out


# ---------------------------------------------------------------------------
# Module-order routing for "what's the next lab?" questions.
#
# "Which lab comes after X" is a CURRICULUM question — its answer is the fixed
# course order, not anything inside a testcase. Left to normal retrieval it's
# unreliable: a testcase's own "Next Step" section (meaning the next step WITHIN
# that lab) can outrank the right content, so the same question flips between
# "next lab = Floorplan" and "next = the challenge testcase". We answer it
# deterministically from the ordered curriculum below (mirrors the
# available-now modules of pd_learning_path.html, in stage order). Tool-agnostic
# — the module order is identical for Synopsys and Cadence — so no tool clarify.
# ---------------------------------------------------------------------------
_CURRICULUM = [
    ("SYN", "Synthesis", "Unresolved References"),
    ("SYN", "Synthesis", "Check Timing Issues"),
    ("SYN", "Synthesis", "Logic Aware Synthesis"),
    ("SYN", "Synthesis", "Physical Aware Synthesis"),
    ("LEC", "LEC", "RTL to Netlist"),
    ("LEC", "LEC", "Netlist to Netlist"),
    ("LEC", "LEC", "Clock Gating App Option"),
    ("PNR", "PnR", "Design Init"),
    ("PNR", "PnR", "Floorplan"),
    ("PNR", "PnR", "Placement"),
    ("PNR", "PnR", "CTS"),
    ("PNR", "PnR", "Route"),
    ("PNR", "PnR", "Post Route"),
    ("PNR", "PnR", "Chip Finish"),
    ("PV", "PV", "DRC Flow"),
    ("PV", "PV", "LVS Flow"),
    ("PV", "PV", "Antenna"),
    ("PV", "PV", "Dummy Fill FEOL"),
    ("PV", "PV", "Dummy Fill BEOL"),
    ("PV", "PV", "GDS Merging"),
    ("STA", "STA", "STA Flow"),
    ("STA", "STA", "DMSA Based ECO Generation"),
]
_STAGE_NUM = {"SYN": 1, "LEC": 2, "PNR": 3, "PV": 4, "STA": 5}

# Alias patterns per module. Matching picks the LONGEST match so "post route"
# resolves to Post Route rather than Route.
_MODULE_ALIASES = {
    "Unresolved References": r"unresolved\s+ref\w*|missing\s+ref\w*|unresolved",
    "Check Timing Issues":   r"check\s+timing|timing\s+issue\w*",
    "Logic Aware Synthesis": r"logic[\s-]*aware(?:\s+synth\w*)?",
    "Physical Aware Synthesis": r"physical[\s-]*aware(?:\s+synth\w*)?",
    "RTL to Netlist":  r"rtl[\s-]*to[\s-]*netlist|rtl2netlist|\br2n\b",
    "Netlist to Netlist": r"netlist[\s-]*to[\s-]*netlist|\bn2n\b",
    "Clock Gating App Option": r"clock[\s-]*gating(?:\s+app\w*)?",
    "Design Init":  r"design\s*init\w*|design\s+initial\w*|initializ\w*|\bdi\b|\binit\b",
    "Floorplan":    r"floor[\s-]*plan\w*|\bfp\b",
    "Placement":    r"placement|\bplacing\b|\bplace\b|\bpl\b",
    "CTS":          r"\bcts\b|clock\s+tree(?:\s+synth\w*)?",
    "Route":        r"\brout(?:e|ing)\b|detail\s+rout\w*|\brt\b",
    "Post Route":   r"post[\s-]*rout\w*",
    "Chip Finish":  r"chip\s*finish\w*|\bcf\b",
    "DRC Flow":     r"\bdrc\b",
    "LVS Flow":     r"\blvs\b",
    "Antenna":      r"antenna",
    "Dummy Fill FEOL": r"dummy\s*fill\s*feol|feol\s*(?:fill|dummy)|\bfeol\b",
    "Dummy Fill BEOL": r"dummy\s*fill\s*beol|beol\s*(?:fill|dummy)|\bbeol\b",
    "GDS Merging":  r"gds\s*merg\w*|merg\w*\s*gds",
    "STA Flow":     r"\bsta\s*flow\b|static\s+timing(?:\s+analysis)?",
    "DMSA Based ECO Generation": r"\bdmsa\b|eco\s+generation",
}

_NEXT_INTENT_RE = re.compile(
    r"\b(next|after|following|subsequent|proceed|onwards?)\b"
    r"|what\s+now|now\s+what|move\s+on|where\s+to\b", re.I)
# If the question is command/step scoped, it isn't a curriculum "next lab" ask.
_CMD_SCOPE_RE = re.compile(
    r"\b(command|cmd|script|run|execute|syntax|option|flag|tcl|step)\b", re.I)


def _match_module(text):
    """Return the canonical module named in text (longest alias match), or None."""
    best = None  # (match_len, canonical_name)
    for canon, pat in _MODULE_ALIASES.items():
        m = re.search(pat, text, re.I)
        if m and (best is None or (m.end() - m.start()) > best[0]):
            best = (m.end() - m.start(), canon)
    return best[1] if best else None


def _stage_order(stage_code):
    """Ordered module names of one stage, for showing the sequence."""
    return [m for (sc, _, m) in _CURRICULUM if sc == stage_code]


def _next_lab_route(question):
    """If the question asks which lab/module comes next after a named module,
    answer deterministically from _CURRICULUM. Else None (fall through to RAG).
    """
    if _CMD_SCOPE_RE.search(question) or not _NEXT_INTENT_RE.search(question):
        return None
    module = _match_module(question)
    if not module:
        return None
    idx = next((i for i, (_, _, m) in enumerate(_CURRICULUM) if m == module), None)
    if idx is None:
        return None

    sc, slabel, mod = _CURRICULUM[idx]
    snum = _STAGE_NUM[sc]
    order = " → ".join(_stage_order(sc))
    if idx == len(_CURRICULUM) - 1:
        answer = (
            f"You've completed **{mod}** — the final lab in the course "
            f"(Stage {snum}, {slabel}). That's the end of the physical-design "
            f"learning path. 🎉\n\n{slabel} order: {order}")
    else:
        nsc, nslabel, nmod = _CURRICULUM[idx + 1]
        if nsc == sc:
            answer = (
                f"You finished **{mod}**. The next lab in **Stage {snum} — "
                f"{slabel}** is **{nmod}**.\n\n{slabel} order: {order}\n\n"
                f"Each module has a **Guided** lab (step-by-step) then a "
                f"**Challenge** lab (open-ended), so do {nmod} Guided next.")
        else:
            nnum = _STAGE_NUM[nsc]
            answer = (
                f"You finished **{mod}**, the last lab in **Stage {snum} — "
                f"{slabel}**. Next is **Stage {nnum} — {nslabel}**, starting "
                f"with **{nmod}**.\n\n{nslabel} order: "
                f"{' → '.join(_stage_order(nsc))}")

    source = {
        "n": 1,
        "lab_name": "Recommended Learning Path",
        "heading": f"Stage {snum} — {slabel}",
        "source": OVERVIEW_SOURCE,
        "score": 1.0,
        "facets": {},
        "content": f"{slabel} modules, in order: {order}",
    }
    return {"answer": answer, "sources": [source]}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "rag.html", {"request": request, "labs": pd_engine.labs}
    )


@app.get("/api/labs")
def api_labs():
    return {"labs": pd_engine.labs, "chunks": len(pd_engine.chunks)}


def _classify_question(question: str) -> dict:
    """
    Classify if question is platform-related, domain-related, or both.

    Returns:
        {
            "search_platform": bool,  # Search platform FAQ database
            "search_domain": bool,     # Search domain-specific database
            "reasoning": str           # Why this classification was chosen
        }
    """
    prompt = f"""You are a question classifier for SemiconLabs platform.

Classify this question into one of three categories:

1. **PLATFORM** - Questions about the SemiconLabs platform itself:
   - Dashboard, navigation, UI (Home, Skills, Journey, Profile)
   - Plans & subscriptions (Basic vs Pro, upgrades, renewals)
   - Account management (password reset, profile updates)
   - Certificates (download, LinkedIn sharing)
   - Labs platform features (provisioning, DCV login, data backup)
   - Enrollment, competencies, quizzes

2. **DOMAIN** - Technical questions about VLSI/semiconductor content:
   - Physical Design (PD): synthesis, clock tree, placement, routing, STA, PV, LEC
   - Design Verification (DV): binary numbers, gates, boolean logic, Karnaugh maps
   - Analog Layout: MOSFET, transistors, wafer fabrication, power dissipation
   - EDA tools usage: ICC2, Innovus, PrimeTime, Calibre commands and flows

3. **BOTH** - Questions that could involve both platform AND domain knowledge

Question: "{question}"

Respond in JSON format:
{{
    "category": "PLATFORM" | "DOMAIN" | "BOTH",
    "reasoning": "brief explanation"
}}"""

    try:
        response = _client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=150
        )

        result_text = response.choices[0].message.content.strip()
        # Parse JSON response
        import json
        result = json.loads(result_text)

        category = result.get("category", "DOMAIN")
        reasoning = result.get("reasoning", "")

        return {
            "search_platform": category in ("PLATFORM", "BOTH"),
            "search_domain": category in ("DOMAIN", "BOTH"),
            "reasoning": reasoning
        }
    except Exception as e:
        # Default to searching domain only if classifier fails
        return {
            "search_platform": False,
            "search_domain": True,
            "reasoning": f"Classifier error: {e}, defaulting to domain search"
        }


@app.post("/api/ask")
def api_ask(req: AskRequest):
    # Initialize token tracker
    tracker = TokenTracker(req.question)

    # Classify question: platform vs domain vs both
    classification = _classify_question(req.question)

    # If platform-only question, search platform database
    if classification["search_platform"] and not classification["search_domain"]:
        platform_hits = platform_engine.search(req.question, k=3)
        if not platform_hits:
            return {
                "answer": "No platform FAQ content found for this question.",
                "sources": [],
                "domain": "platform",
                "classification": classification
            }

        # Build answer from platform FAQs
        context_parts = []
        for i, hit in enumerate(platform_hits, 1):
            context_parts.append(
                f"[{i}] Category: {hit['category']}\n"
                f"Question: {hit['question']}\n"
                f"Answer: {hit['answer']}\n"
            )

        context = "\n\n".join(context_parts)
        prompt = f"""Answer the user's question based on the platform FAQ content below.

Platform FAQ Content:
{context}

User Question: {req.question}

Instructions:
1. Provide a clear, complete answer based on the FAQ content above
2. Include ALL relevant details from the FAQs (steps, locations, requirements, etc.)
3. If the FAQ mentions WHERE to do something (e.g., Journey page, Profile section), include that in your answer
4. Be comprehensive but concise - don't leave out important information"""

        response = _client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500
        )

        answer = response.choices[0].message.content.strip()
        tracker.track_answer(response.usage)

        # Format sources for consistency
        sources = [{
            "n": i,
            "category": hit["category"],
            "question": hit["question"],
            "answer": hit["answer"],
            "score": hit["score"]
        } for i, hit in enumerate(platform_hits, 1)]

        # Save token stats
        _token_stats.append({
            "timestamp": tracker.started_at.isoformat(),
            "question": req.question[:100],
            "domain": "platform",
            **tracker.get_summary()
        })

        # Save to question history
        _question_history.append({
            "question": req.question,
            "domain": "platform",
            "timestamp": tracker.started_at.isoformat(),
            "answer_preview": answer[:100] + "..." if len(answer) > 100 else answer
        })

        return {
            "answer": answer,
            "sources": sources,
            "domain": "platform",
            "token_usage": tracker.get_summary(),
            "classification": classification
        }

    # Select the appropriate RAG engine based on domain
    domain = req.domain.lower()
    if domain not in RAG_ENGINES:
        return {
            "error": f"Invalid domain '{domain}'. Choose 'pd' (Physical Design), 'dv' (Design Verification), or 'analog' (Analog Layout)",
            "sources": []
        }

    engine = RAG_ENGINES[domain]

    # DV and Analog domains don't have clarify/facets/next-lab routing - skip PD-specific logic
    if domain in ("dv", "analog"):
        # Simple flow: retrieve and answer
        hits = engine.search(req.question, k=max(req.k, RETRIEVE_K))
        if not hits:
            domain_label = "DV" if domain == "dv" else "Analog Layout"
            return {"answer": f"No {domain_label} content found for this question.", "sources": [], "domain": domain}
    else:
        # PD domain - full flow with clarify/facets/routing
        # 0a. "Which lab comes after X?" is a curriculum-order question — answer it
        #     deterministically from the course sequence before any retrieval, so it
        #     never gets hijacked by a testcase's internal "Next Step" section.
        routed = _next_lab_route(req.question)
        if routed:
            return routed

        # 0b. If the question already names a vendor-specific tool (e.g. ICC2 =
        #    Synopsys, Innovus = Cadence), the tool is settled — resolve the provider
        #    silently so retrieval restricts to that vendor and we skip a redundant
        #    "Synopsys or Cadence?" clarify. Only when the user hasn't picked one.
        if not (req.facets or {}).get("provider"):
            implied = _provider_from_tool(req.question)
            if implied:
                req.facets = {**(req.facets or {}), "provider": implied}

        # 1. Retrieve wide (precise sub-chunks). A picked facet hard-restricts.
        hits = engine.search(req.question, k=max(req.k, RETRIEVE_K),
                             lab_name=req.lab_name, facets=req.facets)
        if not hits:
            return {"answer": "No lab content is indexed.", "sources": [], "domain": "pd"}

    # 2. Ambiguity check (PD only - DV doesn't have facets/tools)
    overview_mode = False
    if domain == "pd":
        # Learning-path overview page carries no facets; a question is "catalog-level"
        # when that page is the strongest hit OR it asks for one stage's ordered labs
        # (routed by _stage_catalog even if a specific lab ranks #1). Catalog questions
        # are answered from the whole overview page — which already covers every tool
        # and stage — so they never need disambiguation. Only lab-content questions
        # with confident retrieval go through the clarify judgment, which decides
        # (using the real facet spread of the hits) whether a tool/stage choice
        # genuinely changes the answer. The picked option comes back as a facet, so
        # it hard-restricts the corpus on the follow-up request.
        top_is_overview = not (hits[0].get("facets") or {})
        overview_in_hits = any(not (h.get("facets") or {}) for h in hits)
        stage_catalog = _stage_catalog(req.question) \
            if (req.allow_clarify and overview_in_hits) else None
        overview_mode = top_is_overview or stage_catalog is not None

        if req.allow_clarify and not overview_mode \
                and hits[0]["score"] >= MIN_CLARIFY_SCORE:
            clar = _clarify_decision(req.question, hits)
            if clar:
                return {
                    "clarify": True,
                    "question": clar["question"],
                    "options": clar["options"],
                    "facet": clar["facet"],
                    "option_values": clar["option_values"],
                    "sources": [],
                    "domain": "pd"
                }

    # 3. Overview-led catalog question: ground on the WHOLE (tiny) learning-path
    #    page, in document order, so the answer is complete. Skips the top-k
    #    verifier/expander, which can split a section at the chunk boundary and
    #    drop half the stages (e.g. only 3 of the 5 stages surviving).
    fallback = False
    navigate_hint = ""
    partial_answer = False
    if overview_mode:
        sections = _overview_doc_sections()
    else:
        # Progressive evaluation: check 3 chunks at a time
        # Batch 1: chunks 0-2, Batch 2: chunks 3-5
        keep, sufficient, needs_more = [], False, True
        for batch_num in range(2):  # Two batches of 3 chunks
            batch_start = batch_num * BATCH_SIZE
            if batch_start >= len(hits):
                break

            # Evaluate current batch
            batch_keep, batch_sufficient, batch_needs_more, nav_hint = _verify_relevant(
                req.question, hits, batch_start, BATCH_SIZE, tracker, batch_num, domain)

            # Accumulate relevant chunks
            keep.extend(batch_keep)

            # Capture navigation hint if provided
            if nav_hint:
                navigate_hint = nav_hint

            # If sufficient, stop here (token optimization!)
            if batch_sufficient:
                sufficient = True
                needs_more = False
                navigate_hint = ""  # Complete answer, no navigation needed
                break

            # If this batch has relevant chunks but needs more, continue to next batch
            if batch_keep and batch_needs_more:
                needs_more = True
                partial_answer = True
                # Continue to next batch for more context
                if batch_num < 1:  # Still have next batch
                    continue
                # Last batch reached, use what we have + navigation hint
                break

            # If no relevant chunks found in this batch
            if not batch_keep:
                # If first batch found nothing, try second batch
                if batch_num == 0:
                    continue
                # If second batch also found nothing, we're done
                break

        # If nothing passes strict verification, don't dead-end. Fall back to the
        # closest retrieved sections (still ONLY lab content, already restricted
        # to the chosen lab/tool) so the answer can say what ISN'T there AND
        # point to the lab's actual equivalent — e.g. "Synopsys has no clock-tree
        # spec file; CTS there is run via clock_opt." The prompt keeps it honest.
        if not keep:
            keep = list(range(min(3, len(hits))))
            fallback = True
            sufficient = False  # nothing verified -> always expand for context

        # 4. Build the context sections. If the verified sub-chunks are already
        #    sufficient, use them as-is (cheaper, tighter). If NOT sufficient,
        #    expand each winning sub-chunk back to its FULL section (all sibling
        #    chunks sharing the same source+heading) for more context.
        sections, seen = [], set()
        for i in keep:
            h = hits[i]
            source_name = _get_source_name(h, domain)
            # When expanding, all sub-chunks of one section resolve to the same
            # full section, so de-dup by (source, heading). When the sub-chunks are
            # used as-is, each is distinct content, so de-dup by chunk text.
            key = (source_name, h["heading"]) if not sufficient else h["content"]
            if key in seen:
                continue
            seen.add(key)
            content = h["content"] if sufficient else engine.get_section(
                source_name, h["heading"])
            sections.append({
                "lab_name": source_name,  # Keep field name for compatibility
                "heading": h["heading"],
                "source": h["source"],
                "score": round(h["score"], 3),
                "facets": h.get("facets") or {},
                "content": _sanitize_paths(content),
            })

        # Add platform FAQ results if classification says BOTH
        if classification.get("search_platform"):
            platform_hits = platform_engine.search(req.question, k=2)
            for hit in platform_hits:
                sections.append({
                    "lab_name": "Platform FAQ",
                    "heading": hit["category"],
                    "source": "platform",
                    "score": round(hit["score"], 3),
                    "facets": {},
                    "content": f"Q: {hit['question']}\n\nA: {hit['answer']}",
                })

        # Cross-tool probe: when nothing in-scope verified, the asked-about
        # command/term may simply belong to the OTHER vendor's flow (e.g.
        # create_clock_tree_spec is Cadence Innovus; Synopsys ICC2 builds the
        # tree directly via clock_opt). If an unfiltered search ranks another
        # provider's chunk ABOVE every in-scope hit, include that full section
        # as clearly-labelled other-tool context so the answer can name the
        # right tool instead of dead-ending on a bare "not present".
        picked = (req.facets or {}).get("provider")
        if fallback and picked:
            xhits = engine.search(req.question, k=1, lab_name=req.lab_name)
            x = xhits[0] if xhits else None
            xfac = (x.get("facets") or {}) if x else {}
            xprov = xfac.get("provider")
            # Only surface the OTHER vendor's chunk when it's the same STAGE —
            # i.e. that tool's version of the SAME task (CTS Synopsys vs CTS
            # Cadence), not an unrelated stage that merely out-scores a weak
            # in-scope hit (e.g. a Siemens PV antenna check for a CTS question).
            tstage = (hits[0].get("facets") or {}).get("stage")
            if (x and xprov and xprov != picked
                    and xfac.get("stage") and xfac.get("stage") == tstage
                    and x["score"] > hits[0]["score"]):
                sections.append({
                    "lab_name": x["lab_name"],
                    "heading": x["heading"],
                    "source": x["source"],
                    "score": round(x["score"], 3),
                    "facets": x.get("facets") or {},
                    "other_tool": _PROVIDER_LABEL.get(xprov, xprov),
                    "content": _sanitize_paths(
                        engine.get_section(x["lab_name"], x["heading"])),
                })

    # 5. Final answer from the reassembled full sections.
    context = "\n\n".join(
        f"[{n + 1}] ({s['lab_name']} :: {s['heading']})"
        + (f" — NOTE: this section is from the {s['other_tool']} flow, NOT the "
           "tool the user chose" if s.get("other_tool") else "")
        + f"\n{s['content']}"
        for n, s in enumerate(sections)
    )
    other_tool = next((s["other_tool"] for s in sections if s.get("other_tool")),
                      None)
    fallback_note = (
        "\n\nNote: no section directly matches the question. If these sections "
        "do not contain the exact command/term asked about, say plainly that it "
        "is not part of this lab/tool, then briefly state what this lab uses "
        "instead — using ONLY these sections, never outside knowledge."
        + (f" The section marked as from the {other_tool} flow shows the "
           f"asked-about item DOES exist there: say in one sentence that it "
           f"belongs to the {other_tool} flow and name the exact command from "
           f"that section, but do not present {other_tool} steps as part of "
           "the chosen tool's flow." if other_tool else "")
    ) if fallback else ""
    resp = _client.chat.completions.create(
        model=CHAT_MODEL,
        temperature=0.2,
        # Room for v4-flash reasoning tokens plus the final grounded answer.
        max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Context sections:\n\n{context}\n\n"
                        f"Question: {req.question}{fallback_note}"},
        ],
    )

    # Track answer tokens
    if hasattr(resp, 'usage'):
        tracker.track_answer(resp.usage)

    answer = _sanitize_paths(resp.choices[0].message.content)

    sources = [{"n": n + 1, **s} for n, s in enumerate(sections)]
    result = {
        "answer": answer,
        "sources": sources,
        "domain": domain  # Include domain in response
    }

    # Add navigation hint for partial answers or when no relevant content found
    # This helps guide users to complete information
    if navigate_hint:
        result["navigate_hint"] = navigate_hint
        if partial_answer:
            result["partial_answer"] = True
            # Append navigation hint to the answer itself for better UX
            result["answer"] = (
                f"{answer}\n\n💡 **Need more details?** {navigate_hint}"
            )

    # Add token usage statistics
    token_summary = tracker.get_summary()
    result["token_usage"] = {
        "embedding_tokens": token_summary["tokens"]["embedding"],
        "verify_batches": token_summary["tokens"]["verify_batches"],
        "verify_tokens": token_summary["tokens"]["verify_input"] + token_summary["tokens"]["verify_output"],
        "answer_tokens": token_summary["tokens"]["answer_input"] + token_summary["tokens"]["answer_output"],
        "total_tokens": token_summary["tokens"]["total"],
        "cost_usd": round(token_summary["cost_usd"], 6),
        "batches_detail": token_summary["batches_detail"]
    }

    # Save to stats
    tracker.save()

    # Save to question history
    _question_history.append({
        "question": req.question,
        "domain": domain,
        "timestamp": tracker.started_at.isoformat(),
        "answer_preview": answer[:100] + "..." if len(answer) > 100 else answer
    })

    return result


@app.get("/health")
def health():
    return {
        "status": "ok",
        "domains": {
            "pd": {
                "chunks": len(pd_engine.chunks),
                "labs": len(pd_engine.labs)
            },
            "dv": {
                "chunks": len(dv_engine.chunks),
                "modules": len(dv_engine.modules)
            },
            "analog": {
                "chunks": len(analog_engine.chunks),
                "topics": len(analog_engine.topics)
            }
        }
    }


@app.get("/api/history")
def get_history():
    """Get last 10 questions asked in this session."""
    return {
        "total": len(_question_history),
        "questions": list(_question_history)
    }


@app.post("/api/history/clear")
def clear_history():
    """Clear question history."""
    _question_history.clear()
    return {"status": "cleared", "total": 0}


@app.get("/api/query")
def api_query_get(domain: str, question: str):
    """GET endpoint for RAG query - accepts domain and question as query params.

    Example: /api/query?domain=pd&question=How%20to%20do%20CTS%20in%20ICC2

    If clarification is needed (PD domain only), returns:
    {
        "clarify": true,
        "question": "Which tool do you mean?",
        "options": ["Synopsys", "Cadence"],
        "facet": "provider",
        "option_values": ["SNPS", "CDN"]
    }
    """
    # Create an AskRequest object from query params
    req = AskRequest(
        question=question,
        domain=domain,
        k=6,
        allow_clarify=True  # Enable clarify for GET endpoint
    )

    # Use the existing api_ask logic
    result = api_ask(req)

    # If clarification is needed, return full clarify response
    if result.get("clarify"):
        return result

    # Return simplified response with just answer, token usage, and cost
    return {
        "domain": result.get("domain"),
        "question": question,
        "answer": result.get("answer", ""),
        "token_usage": result.get("token_usage", {}),
        "cost_usd": result.get("token_usage", {}).get("cost_usd", 0),
        "sources_count": len(result.get("sources", []))
    }


@app.get("/api/token-stats")
def get_token_stats(limit: int = 100):
    """Get token usage statistics for recent queries."""
    recent = _token_stats[-limit:] if _token_stats else []

    if not recent:
        return {
            "total_queries": 0,
            "queries": [],
            "summary": {}
        }

    # Calculate summary statistics
    total_cost = sum(q["cost_usd"] for q in recent)
    total_tokens = sum(q["tokens"]["total"] for q in recent)
    avg_cost = total_cost / len(recent) if recent else 0
    avg_tokens = total_tokens / len(recent) if recent else 0

    # Count batches usage distribution
    batch_counts = {}
    for q in recent:
        batches = q["tokens"]["verify_batches"]
        batch_counts[batches] = batch_counts.get(batches, 0) + 1

    return {
        "total_queries": len(recent),
        "summary": {
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_per_query_usd": round(avg_cost, 6),
            "total_tokens": total_tokens,
            "avg_tokens_per_query": round(avg_tokens, 2),
            "batch_distribution": batch_counts,
            "optimization_rate": f"{batch_counts.get(1, 0) / len(recent) * 100:.1f}% queries answered in first batch"
        },
        "queries": recent
    }


@app.post("/api/token-stats/clear")
def clear_token_stats():
    """Clear token statistics."""
    global _token_stats
    count = len(_token_stats)
    _token_stats = []
    return {"message": f"Cleared {count} stat entries"}
