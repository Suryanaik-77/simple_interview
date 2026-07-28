"""
Interview Platform — Custom MCP Server

Exposes interview tools via the Model Context Protocol (MCP).
Any MCP-compatible AI client (Claude Desktop, xAI, custom agents) can connect
and use these tools to interact with the interview platform.

Run standalone:  python mcp_server.py
Or as HTTP:      python mcp_server.py --http --port 8010

Tools exposed:
  1. search_question_bank   — find curated interview questions by domain/topic/difficulty
  2. get_candidate_history  — look up a candidate's past interview performance
  3. get_interview_analytics — platform-wide interview statistics
  4. lookup_vlsi_concept    — quick reference for VLSI technical concepts
  5. get_session_review     — get full evaluation for a completed session
"""
import os
import sys
import json
import logging
import argparse

log = logging.getLogger("mcp_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [MCP] %(message)s")

# ── Tool definitions (work with or without MCP SDK) ──────────────────────

TOOLS = [
    {
        "name": "search_question_bank",
        "description": "Search the curated interview question bank. Returns questions matching the given domain, topic, and difficulty level. Use this to find high-quality questions for specific interview scenarios.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": ["physical_design", "analog_layout", "design_verification"],
                    "description": "VLSI domain to search questions for"
                },
                "topic": {
                    "type": "string",
                    "description": "Specific topic (e.g., 'CTS', 'Matching', 'UVM', 'STA'). Optional — omit to search all topics."
                },
                "difficulty": {
                    "type": "string",
                    "enum": ["basic", "intermediate", "advanced", "expert"],
                    "description": "Difficulty level. Optional — omit to search all levels."
                },
                "limit": {
                    "type": "integer",
                    "description": "Max questions to return (default 5)",
                    "default": 5
                }
            },
            "required": ["domain"]
        }
    },
    {
        "name": "get_candidate_history",
        "description": "Look up a candidate's past interview sessions by email. Returns scores, topics covered, strengths/weaknesses from previous interviews. Use this to tailor questions and avoid repeating topics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Candidate email address"
                }
            },
            "required": ["email"]
        }
    },
    {
        "name": "get_interview_analytics",
        "description": "Get platform-wide interview statistics: average scores by domain/level, common weak topics, pass rates. Use for benchmarking a candidate's performance against the pool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter by domain. Optional."
                },
                "level": {
                    "type": "string",
                    "description": "Filter by level. Optional."
                },
                "days": {
                    "type": "integer",
                    "description": "Look back period in days (default 30)",
                    "default": 30
                }
            }
        }
    },
    {
        "name": "lookup_vlsi_concept",
        "description": "Quick technical reference for VLSI concepts. Returns a concise explanation with key points that a candidate should know. Use to verify candidate answers or generate follow-up probes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "concept": {
                    "type": "string",
                    "description": "VLSI concept to look up (e.g., 'clock tree synthesis', 'OCV', 'common centroid', 'UVM scoreboard')"
                },
                "level": {
                    "type": "string",
                    "enum": ["junior", "senior"],
                    "description": "Expected knowledge depth",
                    "default": "junior"
                }
            },
            "required": ["concept"]
        }
    },
    {
        "name": "get_session_review",
        "description": "Get the full evaluation and per-question breakdown for a completed interview session. Returns scores, strengths, weaknesses, and topic performance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to look up"
                }
            },
            "required": ["session_id"]
        }
    },
]


# ── Tool implementations ─────────────────────────────────────────────────

def _ensure_db():
    import database
    if not database.is_available():
        database.init_db()
    return database


def tool_search_question_bank(domain, topic=None, difficulty=None, limit=5):
    db = _ensure_db()
    questions = db.get_question_bank(domain=domain, topic=topic, difficulty=difficulty, limit=limit)
    if not questions:
        return {"results": [], "message": f"No questions found for {domain}" + (f"/{topic}" if topic else "")}
    return {
        "results": [
            {
                "id": q["id"],
                "topic": q["topic"],
                "difficulty": q["difficulty"],
                "question": q["question_text"],
                "expected_points": q.get("expected_points", []),
            }
            for q in questions
        ],
        "count": len(questions),
    }


def tool_get_candidate_history(email):
    db = _ensure_db()
    if not db.is_available():
        return {"found": False, "error": "Database not available"}
    from database import get_conn
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT session_id, session_summary
                    FROM candidate_history
                    WHERE email = %s
                    ORDER BY created_at DESC
                    LIMIT 5
                """, (email,))
                rows = cur.fetchall()
        if not rows:
            return {"found": False, "message": f"No history for {email}"}
        sessions = []
        for r in rows:
            summary = r["session_summary"] if isinstance(r["session_summary"], dict) else json.loads(r["session_summary"])
            sessions.append({
                "session_id": r["session_id"],
                "score": summary.get("overall_score"),
                "recommendation": summary.get("recommendation"),
                "domain": summary.get("domain"),
                "level": summary.get("level"),
                "questions_count": len(summary.get("questions_asked", [])),
                "strengths": summary.get("strengths", []),
                "weaknesses": summary.get("weaknesses", []),
            })
        return {"found": True, "email": email, "sessions": sessions, "total_interviews": len(sessions)}
    except Exception as e:
        return {"found": False, "error": str(e)}


def tool_get_interview_analytics(domain=None, level=None, days=30):
    db = _ensure_db()
    if not db.is_available():
        return {"error": "Database not available"}
    from database import get_conn
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                conditions = ["session_data->>'phase' = 'ended'",
                              f"updated_at >= NOW() - '{days} days'::interval"]
                params = []
                if domain:
                    conditions.append("session_data->'resume'->>'domain' = %s")
                    params.append(domain)
                if level:
                    conditions.append("session_data->'resume'->>'level' = %s")
                    params.append(level)
                where = " AND ".join(conditions)
                cur.execute(f"""
                    SELECT
                        COUNT(*) as total_sessions,
                        AVG((session_data->'evaluation'->>'overall_score')::float) as avg_score,
                        AVG((session_data->'evaluation'->>'communication_score')::float) as avg_comm_score
                    FROM active_sessions
                    WHERE {where}
                      AND session_data->'evaluation'->>'status' = 'done'
                """, params)
                stats = dict(cur.fetchone() or {})

                cur.execute(f"""
                    SELECT
                        session_data->'evaluation'->>'recommendation' as rec,
                        COUNT(*) as cnt
                    FROM active_sessions
                    WHERE {where}
                      AND session_data->'evaluation'->>'status' = 'done'
                    GROUP BY rec
                """, params)
                rec_dist = {r["rec"]: r["cnt"] for r in cur.fetchall()}

        return {
            "period_days": days,
            "domain": domain or "all",
            "level": level or "all",
            "total_sessions": stats.get("total_sessions", 0),
            "avg_score": round(float(stats.get("avg_score") or 0), 1),
            "avg_communication_score": round(float(stats.get("avg_comm_score") or 0), 1),
            "recommendation_distribution": rec_dist,
        }
    except Exception as e:
        return {"error": str(e)}


VLSI_CONCEPTS = {
    "clock tree synthesis": {
        "junior": "CTS builds a balanced clock distribution network after placement. Key points: clock skew minimization, insertion delay, buffer/inverter selection, useful skew for timing optimization. A junior should know what skew is, how CTS tools balance it, and basic debug (checking skew reports, buffer chain).",
        "senior": "CTS builds balanced clock networks. Seniors should know: multi-source CTS, mesh vs tree tradeoffs, NDR for clock nets, OCV impact on clock paths, generated clock handling, CTS constraints (max_transition, max_fanout, max_cap), useful skew exploitation, and post-CTS optimization flow."
    },
    "ocv": {
        "junior": "On-Chip Variation (OCV) models process/voltage/temperature variation within a single chip. Setup: late data + early clock. Hold: early data + late clock. AOCV/POCV provide path-depth-aware derating instead of flat worst-case.",
        "senior": "OCV/AOCV/POCV/SOCV model intra-die variation. Seniors must know: why flat OCV is pessimistic, how AOCV uses depth-based tables, POCV uses statistical distributions, stage-count vs distance effects, and how to generate/validate AOCV tables from foundry data."
    },
    "common centroid": {
        "junior": "Common centroid layout places matched devices symmetrically around a center point to cancel linear gradient effects. Used for diff pairs, current mirrors, and capacitor arrays.",
        "senior": "Common centroid cancels first-order gradients. Seniors should know: when interdigitation is sufficient vs full CC, CC for 2D gradients, optimal CC patterns (ABBAABBA vs ABBA), dummy device placement, routing symmetry requirements, and CC limitations (doesn't cancel random mismatch)."
    },
    "uvm scoreboard": {
        "junior": "UVM scoreboard compares DUT output against a reference model. Key: TLM analysis ports for monitor-to-scoreboard communication, expected vs actual comparison, in-order vs out-of-order checking.",
        "senior": "Scoreboard architecture decisions: in-order FIFO vs associative array for OOO protocols, per-channel tracking for multi-stream DUTs, end-of-test drain checks, partial match policies, and performance impact of complex scoreboards on simulation speed."
    },
    "setup hold": {
        "junior": "Setup time: data must be stable before clock edge. Hold time: data must be stable after clock edge. Setup violation → increase data path delay or decrease clock period. Hold violation → add buffers in data path.",
        "senior": "Setup = Tclk + Tskew > Tdata + Tsetup. Hold = Tdata + Thold > Tskew. Seniors should know: multi-corner analysis, OCV effects on setup/hold simultaneously, useful skew for setup at the cost of hold margin, temperature inversion effects below 28nm, and ECO strategies."
    },
    "ir drop": {
        "junior": "IR drop = voltage drop across power grid resistance. Static IR: average current. Dynamic IR: peak switching current. Causes timing failures because lower voltage = slower cells. Fix: add power straps, vias, decaps.",
        "senior": "IR drop analysis: static (Ohm's law) vs dynamic (current waveform × impedance). EM limits on power straps. Package inductance effects (Ldi/dt). Decap placement strategy. Rush current during clock edge. IR-aware timing analysis. Power grid EM signoff."
    },
    "sta": {
        "junior": "Static Timing Analysis checks all paths without simulation. Reports setup/hold slack. Negative slack = violation. Key: understanding timing reports, path groups, clock domains, false/multi-cycle paths.",
        "senior": "STA: graph-based (GPSTA) vs path-based (CPPR). MMMC (multi-mode multi-corner). Seniors must know: CPPR, reconvergent clock pessimism removal, SI-aware STA, AOCV/POCV derating, signoff corners selection rationale, and timing ECO methodology."
    },
    "floorplanning": {
        "junior": "Floorplanning: macro placement, power planning, pin assignment, aspect ratio. Goals: minimize wirelength, ensure routability, meet timing. Key: channel spacing, macro orientation, flyline analysis.",
        "senior": "Floorplanning drives PPA. Seniors should know: hierarchical vs flat, partition strategies, power domain planning, voltage island floorplanning, IO planning for package, multi-supply power grid architecture, and early congestion estimation."
    },
    "matching": {
        "junior": "Matching techniques: common centroid, interdigitation, dummy devices, same orientation, guard rings. Goal: cancel systematic and random mismatch for analog accuracy.",
        "senior": "Matching: Pelgrom model (σ ∝ 1/√(WL)), systematic vs random sources. WPE (well proximity effect), STI stress, LOD (length of diffusion), contact resistance variation. Layout techniques: CC, interdigitation, dummies, symmetric routing, kelvin connections."
    },
    "formal verification": {
        "junior": "Formal verification mathematically proves properties without simulation. Bounded vs unbounded proofs. Key: assertions (SVA), assumptions, cover properties. Limitations: state space explosion for complex designs.",
        "senior": "Formal: model checking (exhaustive state space), equivalence checking (RTL vs netlist). Techniques: abstractions, constraints, decomposition. When to use formal vs simulation. Bounded proofs interpretation. Formal for connectivity, deadlock, protocol compliance."
    },
    "esd": {
        "junior": "ESD protection: diodes/clamps at IO pads to shunt discharge current. HBM (Human Body Model) and CDM (Charged Device Model) standards. Tradeoff: protection level vs parasitic capacitance on signal pins.",
        "senior": "ESD: HBM, CDM, MM models. Primary + secondary protection. Clamp design (GGNMOS, SCR). CDM protection for internal nets. ESD rule checking in layout. ESD current paths analysis. Impact on high-speed IO (capacitance budget). Whole-chip ESD verification."
    },
}


_CONCEPT_ALIASES = {
    "cts": "clock tree synthesis", "clock tree": "clock tree synthesis",
    "on chip variation": "ocv", "aocv": "ocv", "pocv": "ocv",
    "cc layout": "common centroid", "centroid": "common centroid",
    "scoreboard": "uvm scoreboard", "uvm": "uvm scoreboard",
    "setup": "setup hold", "hold": "setup hold", "setup time": "setup hold", "hold time": "setup hold",
    "ir": "ir drop", "power drop": "ir drop",
    "static timing": "sta", "timing analysis": "sta",
    "floorplan": "floorplanning", "floor plan": "floorplanning",
    "match": "matching", "mismatch": "matching",
    "formal": "formal verification", "model checking": "formal verification",
    "electrostatic discharge": "esd", "esd protection": "esd",
}


def tool_lookup_vlsi_concept(concept, level="junior"):
    key = concept.lower().strip()
    # Direct match
    if key in VLSI_CONCEPTS:
        v = VLSI_CONCEPTS[key]
        return {"concept": concept, "level": level, "explanation": v.get(level, v.get("junior", "")), "source": "interview_platform_reference"}
    # Alias match
    if key in _CONCEPT_ALIASES:
        resolved = _CONCEPT_ALIASES[key]
        v = VLSI_CONCEPTS.get(resolved, {})
        return {"concept": concept, "matched": resolved, "level": level, "explanation": v.get(level, v.get("junior", "")), "source": "interview_platform_reference"}
    # Substring match
    for k, v in VLSI_CONCEPTS.items():
        if k in key or key in k:
            return {"concept": concept, "matched": k, "level": level, "explanation": v.get(level, v.get("junior", "")), "source": "interview_platform_reference"}
    # Fuzzy — any word matches
    words = key.split()
    for k, v in VLSI_CONCEPTS.items():
        if any(w in k for w in words if len(w) > 2):
            return {"concept": concept, "matched": k, "level": level, "explanation": v.get(level, v.get("junior", "")), "source": "interview_platform_reference"}
    # Check aliases fuzzy
    for alias, resolved in _CONCEPT_ALIASES.items():
        if any(w in alias for w in words if len(w) > 2):
            v = VLSI_CONCEPTS.get(resolved, {})
            return {"concept": concept, "matched": resolved, "level": level, "explanation": v.get(level, v.get("junior", "")), "source": "interview_platform_reference"}
    return {"concept": concept, "found": False, "message": "Concept not in reference database. Available: " + ", ".join(VLSI_CONCEPTS.keys())}


def tool_get_session_review(session_id):
    db = _ensure_db()
    if not db.is_available():
        return {"found": False, "error": "Database not available"}
    from database import get_conn
    try:
        from psycopg.rows import dict_row
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT session_data FROM active_sessions WHERE session_id = %s", (session_id,))
                row = cur.fetchone()
        if not row:
            return {"found": False, "message": f"Session {session_id} not found"}
        data = row["session_data"] if isinstance(row["session_data"], dict) else json.loads(row["session_data"])
        ev = data.get("evaluation", {})
        if not ev or ev.get("status") != "done":
            return {"found": True, "status": ev.get("status", "not_evaluated"), "message": "Evaluation not complete"}
        return {
            "found": True,
            "session_id": session_id,
            "candidate": data.get("resume", {}).get("candidate_name", "Unknown"),
            "domain": data.get("resume", {}).get("domain"),
            "level": data.get("resume", {}).get("level"),
            "overall_score": ev.get("overall_score"),
            "communication_score": ev.get("communication_score"),
            "recommendation": ev.get("recommendation"),
            "verdict": ev.get("verdict"),
            "strengths": ev.get("strengths", []),
            "weaknesses": ev.get("weaknesses", []),
            "per_question": ev.get("per_question", []),
            "topic_breakdown": ev.get("topic_breakdown", []),
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


# ── Dispatch ─────────────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "search_question_bank": lambda args: tool_search_question_bank(**args),
    "get_candidate_history": lambda args: tool_get_candidate_history(**args),
    "get_interview_analytics": lambda args: tool_get_interview_analytics(**args),
    "lookup_vlsi_concept": lambda args: tool_lookup_vlsi_concept(**args),
    "get_session_review": lambda args: tool_get_session_review(**args),
}


def call_tool(name, arguments):
    """Call a tool by name with arguments. Returns JSON-serializable result."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(arguments)
    except Exception as e:
        log.error(f"Tool {name} failed: {e}")
        return {"error": str(e)}


def get_tools_for_llm():
    """Return tool definitions in OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],
            }
        }
        for t in TOOLS
    ]


# ── MCP Protocol Server (stdio transport) ────────────────────────────────

def run_mcp_stdio():
    """Run as an MCP server over stdio (for Claude Desktop, etc.)."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp import types
    except ImportError:
        log.error("MCP SDK not installed. Run: pip install mcp")
        log.info("Falling back to HTTP mode. Use --http flag.")
        return

    server = Server("interview-platform")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOLS
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        result = call_tool(name, arguments or {})
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    import asyncio
    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(main())


def run_http_server(port=8010):
    """Run as an HTTP server for direct API access and testing."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn

    http_app = FastAPI(title="Interview Platform MCP Server")
    http_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @http_app.get("/tools")
    def list_tools():
        return {"tools": TOOLS}

    @http_app.post("/call/{tool_name}")
    def call(tool_name: str, args: dict = {}):
        return call_tool(tool_name, args)

    @http_app.get("/health")
    def health():
        return {"status": "ok", "tools": len(TOOLS)}

    log.info(f"MCP HTTP server starting on port {port}")
    uvicorn.run(http_app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interview Platform MCP Server")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    parser.add_argument("--port", type=int, default=8010, help="HTTP port (default 8010)")
    args = parser.parse_args()

    if args.http:
        run_http_server(args.port)
    else:
        run_mcp_stdio()
