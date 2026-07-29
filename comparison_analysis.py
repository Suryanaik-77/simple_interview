"""
Comparison Analysis Module
Compares a candidate's current interview with their previous interview(s)
to identify improvements and areas that still need work.
"""
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from openai import OpenAI

log = logging.getLogger('interview')


def compare_interviews(
    current_session: Dict[str, Any],
    previous_sessions: List[Dict[str, Any]],
    openai_client: OpenAI,
    model: str = "gpt-4o-mini"
) -> Dict[str, Any]:
    """
    Compare current interview with previous interview(s).

    Args:
        current_session: The current/latest interview session data
        previous_sessions: List of previous session summaries (from candidate_history)
        openai_client: OpenAI client for LLM-based analysis
        model: Model to use for comparison analysis

    Returns:
        Dictionary containing detailed comparison analysis
    """
    if not previous_sessions:
        return {
            "status": "no_history",
            "message": "This is the candidate's first interview. No comparison available."
        }

    # Get the most recent previous session
    most_recent_previous = previous_sessions[-1]

    # Extract current interview data
    current_eval = current_session.get("evaluation", {}) or {}
    current_conversation = current_session.get("conversation", [])

    # Safely get scores (handle None values)
    current_score = current_eval.get("overall_score")
    if current_score is None:
        current_score = 0

    previous_eval = most_recent_previous.get("evaluation", {}) or {}
    previous_score = previous_eval.get("overall_score")
    if previous_score is None:
        previous_score = 0

    # Build comparison data structure
    comparison = {
        "status": "success",
        "current_session": {
            "session_id": current_session.get("id"),
            "date": datetime.now().isoformat(),
            "domain": current_session.get("resume", {}).get("domain", ""),
            "level": current_session.get("level", ""),
            "turns": current_session.get("turn", 0),
            "overall_score": current_score,
            "recommendation": current_eval.get("recommendation", ""),
            "level_fit": current_eval.get("level_fit", ""),
        },
        "previous_session": {
            "session_id": most_recent_previous.get("session_id"),
            "date": most_recent_previous.get("date"),
            "domain": most_recent_previous.get("domain", ""),
            "turns": most_recent_previous.get("turns", 0),
            "overall_score": previous_score,
            "recommendation": previous_eval.get("recommendation", ""),
            "level_fit": previous_eval.get("level_fit", ""),
        }
    }

    # Calculate quantitative improvements (safe division)
    score_delta = current_score - previous_score
    if previous_score > 0:
        percentage_change = round((score_delta / previous_score) * 100, 2)
    else:
        percentage_change = 0.0

    comparison["score_improvement"] = {
        "delta": round(score_delta, 2),
        "percentage_change": percentage_change,
        "improved": score_delta > 0
    }

    # Topic-level comparison
    comparison["topics"] = _compare_topics(
        current_session.get("conversation", []),
        most_recent_previous.get("topics_asked", []),
        most_recent_previous.get("questions_asked", [])
    )

    # Skills comparison
    comparison["skills_coverage"] = _compare_skills(
        current_session.get("resume", {}).get("skills", []),
        current_conversation,
        most_recent_previous.get("skills", [])
    )

    # Generate LLM-based qualitative analysis
    comparison["insights"] = _generate_comparison_insights(
        current_session,
        current_eval,
        most_recent_previous,
        openai_client,
        model
    )

    # Total interview count
    comparison["interview_count"] = len(previous_sessions) + 1

    return comparison


def _extract_topic_from_question(question: str) -> str:
    """Extract topic from question using keyword matching."""
    if not question:
        return ""

    q = question.lower()

    # Design Verification topics
    dv_topics = {
        "covergroup": "Functional Coverage", "coverage": "Functional Coverage",
        "mailbox": "Communication", "queue": "Data Structures",
        "scoreboard": "Verification Components", "interface": "Interfaces",
        "constraint": "Constrained Random", "random": "Constrained Random",
        "sequence": "UVM Sequences", "sequencer": "UVM Sequencer",
        "uvm": "UVM Framework", "assertion": "Assertions",
        "sva": "SystemVerilog Assertions", "clock domain": "Clock Domain Crossing",
        "cdc": "Clock Domain Crossing", "phase": "UVM Phases",
        "testbench": "Testbench Architecture", "fifo": "FIFO Design",
        "alu": "ALU Design",
    }

    # Physical Design topics
    pd_topics = {
        "floorplan": "Floorplanning", "placement": "Placement",
        "cts": "Clock Tree Synthesis", "clock tree": "Clock Tree Synthesis",
        "routing": "Routing", "sta": "Static Timing Analysis",
        "timing": "Timing Analysis", "setup": "Timing Constraints",
        "hold": "Timing Constraints", "power": "Power Optimization",
        "ir drop": "IR Drop", "lvs": "LVS Checking", "drc": "DRC Rules",
    }

    # Analog topics
    analog_topics = {
        "opamp": "Opamp Design", "comparator": "Comparator Design",
        "bandgap": "Bandgap Reference", "pll": "PLL Design",
        "adc": "ADC Design", "dac": "DAC Design",
    }

    all_topics = {**dv_topics, **pd_topics, **analog_topics}

    for keyword, topic in all_topics.items():
        if keyword in q:
            return topic

    return "General"


def _compare_topics(
    current_conversation: List[Dict],
    previous_topics: List[str],
    previous_questions: List[str]
) -> Dict[str, Any]:
    """Compare topics covered in current vs previous interview."""
    # Extract topics from current conversation
    current_topics = []
    for turn in current_conversation:
        if turn.get("question"):
            # Try to get topic from turn, otherwise extract it
            topic = turn.get("topic") or _extract_topic_from_question(turn.get("question", ""))
            if topic:
                current_topics.append(topic)

    current_topics_set = set(filter(None, current_topics))
    previous_topics_set = set(filter(None, previous_topics))

    return {
        "current_topics": list(current_topics_set),
        "previous_topics": list(previous_topics_set),
        "new_topics": list(current_topics_set - previous_topics_set),
        "repeated_topics": list(current_topics_set & previous_topics_set),
        "topics_expanded": len(current_topics_set) > len(previous_topics_set),
    }


def _compare_skills(
    current_skills: List[str],
    current_conversation: List[Dict],
    previous_skills: List[str]
) -> Dict[str, Any]:
    """Compare skills coverage between interviews."""
    current_skills_set = set(current_skills)
    previous_skills_set = set(previous_skills)

    # Check which skills were actually discussed in the current interview
    discussed_skills = set()
    for turn in current_conversation:
        question = turn.get("question", "").lower()
        answer = turn.get("answer", "").lower()
        for skill in current_skills:
            if skill.lower() in question or skill.lower() in answer:
                discussed_skills.add(skill)

    return {
        "current_skills": list(current_skills_set),
        "previous_skills": list(previous_skills_set),
        "new_skills_added": list(current_skills_set - previous_skills_set),
        "skills_discussed": list(discussed_skills),
        "skills_coverage_rate": round(len(discussed_skills) / max(len(current_skills_set), 1) * 100, 2)
    }


def _generate_comparison_insights(
    current_session: Dict[str, Any],
    current_eval: Dict[str, Any],
    previous_session: Dict[str, Any],
    openai_client: OpenAI,
    model: str
) -> Dict[str, Any]:
    """Use LLM to generate qualitative comparison insights."""

    # Build context for the LLM
    current_conversation_summary = _summarize_conversation(current_session.get("conversation", []))
    previous_questions = previous_session.get("questions_asked", []) or []

    # Safely get scores
    prev_eval = previous_session.get('evaluation', {}) or {}
    prev_score = prev_eval.get('overall_score')
    prev_score_str = f"{prev_score}/100" if prev_score is not None else "N/A"

    curr_score = current_eval.get('overall_score')
    curr_score_str = f"{curr_score}/100" if curr_score is not None else "N/A"

    prompt = f"""You are an expert technical interviewer analyzing a candidate's progress across multiple interviews.

**Previous Interview (Date: {previous_session.get('date', 'Unknown')}):**
- Domain: {previous_session.get('domain', 'Unknown')}
- Questions Asked: {len(previous_questions)}
- Overall Score: {prev_score_str}
- Recommendation: {prev_eval.get('recommendation', 'N/A')}
- Level Fit: {prev_eval.get('level_fit', 'N/A')}
- Topics Covered: {', '.join(previous_session.get('topics_asked', [])[:10]) if previous_session.get('topics_asked') else 'N/A'}

**Current Interview (Date: {current_session.get('started_at', 'Unknown')}):**
- Domain: {current_session.get('resume', {}).get('domain', 'Unknown')}
- Questions Asked: {current_session.get('turn', 0)}
- Overall Score: {curr_score_str}
- Recommendation: {current_eval.get('recommendation', 'N/A')}
- Level Fit: {current_eval.get('level_fit', 'N/A')}
- Conversation Summary: {current_conversation_summary if current_conversation_summary else 'N/A'}

Based on this comparison, provide:

1. **Improvements** (3-5 specific points where the candidate showed clear progress)
2. **Still Lagging** (3-5 specific areas where the candidate still needs improvement)
3. **New Strengths** (Areas that emerged as new strengths in this interview)
4. **Consistency Check** (Did the candidate maintain their previous strengths?)
5. **Actionable Recommendations** (Top 3 specific actions the candidate should take before the next interview)

Format your response as JSON with the following structure:
{{
    "improvements": ["point 1", "point 2", ...],
    "still_lagging": ["area 1", "area 2", ...],
    "new_strengths": ["strength 1", "strength 2", ...],
    "consistency": {{"maintained": ["strength 1", ...], "lost": ["weakness 1", ...]}},
    "recommendations": ["action 1", "action 2", "action 3"],
    "overall_progress": "brief summary of overall progress trajectory"
}}
"""

    try:
        response = openai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an expert technical interviewer providing detailed, actionable feedback."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}
        )

        import json
        insights = json.loads(response.choices[0].message.content)
        log.info("[Comparison] Generated LLM-based comparison insights")
        return insights

    except Exception as e:
        log.error(f"[Comparison] Failed to generate LLM insights: {e}")
        return {
            "error": "Failed to generate detailed insights",
            "improvements": [],
            "still_lagging": [],
            "new_strengths": [],
            "consistency": {"maintained": [], "lost": []},
            "recommendations": [],
            "overall_progress": "Analysis unavailable"
        }


def _summarize_conversation(conversation: List[Dict]) -> str:
    """Create a brief summary of the conversation for LLM context."""
    qa_pairs = []
    for turn in conversation[:10]:  # First 10 turns
        if turn.get("question"):
            q = turn.get("question", "")[:150]  # Truncate long questions
            a = turn.get("answer", "")[:150]
            score = turn.get("score", "N/A")
            qa_pairs.append(f"Q: {q}\nA: {a}\nScore: {score}")

    return "\n\n".join(qa_pairs)


def generate_comparison_report_text(comparison: Dict[str, Any]) -> str:
    """Generate a human-readable text report from comparison data."""

    if comparison.get("status") == "no_history":
        return comparison.get("message", "No previous history available.")

    insights = comparison.get("insights", {})
    score_improvement = comparison.get("score_improvement", {})

    report_lines = [
        "=" * 80,
        "INTERVIEW COMPARISON ANALYSIS",
        "=" * 80,
        "",
        f"Interview Count: {comparison.get('interview_count', 1)}",
        f"Current Score: {comparison['current_session']['overall_score']}/100",
        f"Previous Score: {comparison['previous_session']['overall_score']}/100",
        f"Score Change: {score_improvement.get('delta', 0):+.2f} ({score_improvement.get('percentage_change', 0):+.2f}%)",
        "",
        "IMPROVEMENTS:",
        "-" * 80,
    ]

    for i, imp in enumerate(insights.get("improvements", []), 1):
        report_lines.append(f"{i}. {imp}")

    report_lines.extend([
        "",
        "STILL LAGGING:",
        "-" * 80,
    ])

    for i, lag in enumerate(insights.get("still_lagging", []), 1):
        report_lines.append(f"{i}. {lag}")

    report_lines.extend([
        "",
        "NEW STRENGTHS:",
        "-" * 80,
    ])

    for i, strength in enumerate(insights.get("new_strengths", []), 1):
        report_lines.append(f"{i}. {strength}")

    report_lines.extend([
        "",
        "ACTIONABLE RECOMMENDATIONS:",
        "-" * 80,
    ])

    for i, rec in enumerate(insights.get("recommendations", []), 1):
        report_lines.append(f"{i}. {rec}")

    report_lines.extend([
        "",
        "OVERALL PROGRESS:",
        "-" * 80,
        insights.get("overall_progress", "N/A"),
        "",
        "=" * 80,
    ])

    return "\n".join(report_lines)
