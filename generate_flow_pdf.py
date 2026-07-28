"""Generate VLSI Interview Platform Flow Architecture PDF."""
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.colors import HexColor, white
from reportlab.pdfgen import canvas

OUTPUT = "interview_flow_architecture.pdf"

BG           = HexColor("#F7F9FC")
TITLE_BG     = HexColor("#1B2A4A")
BOX_BORDER   = HexColor("#2C3E6B")
BOX_BG       = HexColor("#FFFFFF")
ARROW_CLR    = HexColor("#E67E22")
GREEN_CLR    = HexColor("#27AE60")
DARK         = HexColor("#2C3E50")

LIGHT_BLUE   = HexColor("#EBF5FB")
LIGHT_GREEN  = HexColor("#EAFAF1")
LIGHT_ORANGE = HexColor("#FEF9E7")
LIGHT_PURPLE = HexColor("#F4ECF7")

BLUE_H   = HexColor("#2980B9")
GREEN_H  = HexColor("#27AE60")
ORANGE_H = HexColor("#E67E22")
PURPLE_H = HexColor("#8E44AD")
DGREEN_H = HexColor("#1E8449")


def _tri(c, pts):
    p = c.beginPath()
    p.moveTo(pts[0], pts[1]); p.lineTo(pts[2], pts[3]); p.lineTo(pts[4], pts[5]); p.close()
    c.drawPath(p, fill=1, stroke=0)

def rr(c, x, y, w, h, fill=BOX_BG, stroke=BOX_BORDER, sw=1.5):
    c.setStrokeColor(stroke); c.setLineWidth(sw); c.setFillColor(fill)
    c.roundRect(x, y, w, h, 8, fill=1, stroke=1)

def hdr(c, x, y, w, h, text, bg=BLUE_H):
    c.setFillColor(bg); c.roundRect(x, y+h-24, w, 24, 8, fill=1, stroke=0)
    c.setFillColor(bg); c.rect(x, y+h-24, w, 12, fill=1, stroke=0)
    c.setFillColor(white); c.setFont("Helvetica-Bold", 10); c.drawString(x+10, y+h-18, text)

def ad(c, x, y1, y2, clr=None):
    clr = clr or ARROW_CLR; c.setStrokeColor(clr); c.setLineWidth(2.5)
    c.line(x, y1, x, y2+7); c.setFillColor(clr); _tri(c, [x-5, y2+7, x+5, y2+7, x, y2])

def ar(c, x1, x2, y, clr=None):
    clr = clr or ARROW_CLR; c.setStrokeColor(clr); c.setLineWidth(2.5)
    c.line(x1, y, x2-7, y); c.setFillColor(clr); _tri(c, [x2-7, y-5, x2-7, y+5, x2, y])

def al(c, x1, x2, y, clr=None):
    clr = clr or ARROW_CLR; c.setStrokeColor(clr); c.setLineWidth(2.5)
    c.line(x1, y, x2+7, y); c.setFillColor(clr); _tri(c, [x2+7, y-5, x2+7, y+5, x2, y])

def tx(c, x, y, lines, sz=8.5, ld=12):
    for i, ln in enumerate(lines):
        c.setFont("Helvetica", sz); c.setFillColor(DARK)
        c.drawString(x, y - i*ld, ln)


def build_pdf():
    pw, ph = landscape(A3)
    c = canvas.Canvas(OUTPUT, pagesize=landscape(A3))

    c.setFillColor(BG); c.rect(0, 0, pw, ph, fill=1, stroke=0)

    # Title
    c.setFillColor(TITLE_BG); c.rect(0, ph-45, pw, 45, fill=1, stroke=0)
    c.setFillColor(white); c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(pw/2, ph-32, "VLSI Interview Platform  —  Flow Architecture")
    c.setFont("Helvetica", 9); c.drawRightString(pw-20, ph-30, "SpaceX AI  |  2026")

    top = ph - 58
    LOOP_X = 25          # loop arrow runs here (far left)
    LM = 65              # left margin for boxes (leaves space for loop arrow)
    gap = 13
    col1_w = 330
    col2_x = LM + col1_w + 40
    col2_w = pw - col2_x - 30

    # ═══ LEFT COLUMN ═══

    # 1 — Candidate Enters
    h1 = 82; y1 = top - h1
    rr(c, LM, y1, col1_w, h1, fill=LIGHT_BLUE)
    hdr(c, LM, y1, col1_w, h1, "1   CANDIDATE ENTERS", bg=BLUE_H)
    tx(c, LM+12, y1+h1-32, [
        "Browser opens /templates/index.html",
        "Candidate provides:",
        "   Resume (PDF/DOCX)    Domain    Level",
        "   physical_design | analog_layout | design_verification",
    ])

    ad(c, LM+col1_w/2, y1, y1-gap)

    # 2 — Resume Parsing
    h2 = 72; y2 = y1-gap-h2
    rr(c, LM, y2, col1_w, h2, fill=LIGHT_GREEN)
    hdr(c, LM, y2, col1_w, h2, "2   RESUME PARSING", bg=GREEN_H)
    tx(c, LM+12, y2+h2-32, [
        "POST /start-interview",
        "PDF/DOCX --> pdfplumber/docx2txt --> raw text",
        "raw text --> LLM (Haiku) --> structured JSON",
    ])

    ad(c, LM+col1_w/2, y2, y2-gap)

    # 3 — Session Creation
    h3 = 72; y3 = y2-gap-h3
    rr(c, LM, y3, col1_w, h3, fill=LIGHT_ORANGE)
    hdr(c, LM, y3, col1_w, h3, "3   SESSION CREATION", bg=ORANGE_H)
    tx(c, LM+12, y3+h3-32, [
        "session = { session_id, resume, turn: 0,",
        "   conversation: [],  difficulty_level: 1 }",
        "Stored in: Redis (fast) + PostgreSQL (durable)",
    ])

    ad(c, LM+col1_w/2, y3, y3-gap)

    # 4 — Prompt Building
    h4 = 95; y4 = y3-gap-h4
    rr(c, LM, y4, col1_w, h4, fill=LIGHT_PURPLE)
    hdr(c, LM, y4, col1_w, h4, "4   PROMPT BUILDING", bg=PURPLE_H)
    tx(c, LM+12, y4+h4-32, [
        "build_interview_prompt(session)",
        "Loads:  prompts/{level}_{domain}.md",
        "",
        "system = base_prompt (role, rules, phases)",
        "   + candidate_info (name, years, tools, projects)",
        "   + returning_block (if repeat candidate)",
        "   + resume_text (full resume for context)",
    ])

    # Arrow box4 → box5
    a45_y = y4 + h4/2
    ar(c, LM+col1_w, col2_x, a45_y)

    # ═══ RIGHT COLUMN ═══

    # 5 — LLM Generates Question
    h5 = 115; y5 = top - h5
    rr(c, col2_x, y5, col2_w, h5, fill=LIGHT_PURPLE)
    hdr(c, col2_x, y5, col2_w, h5, "5   LLM GENERATES QUESTION", bg=PURPLE_H)
    tx(c, col2_x+12, y5+h5-32, [
        "generate_question()  or  stream_answer() (SSE)",
        "Prompt + history --> call_llm() --> LLM response",
        "  Providers: OpenAI (GPT-4o) | Bedrock (Claude) | Grok (xAI)",
        "",
        "Tag parsing:",
        "  [FOLLOWUP]       --> follow-up, turn stays same",
        "  [END_INTERVIEW]  --> agent ends, go to step 9",
        "  (no tag)          --> main question, turn += 1",
    ])

    ad(c, col2_x+col2_w/2, y5, y5-gap)

    # 6 — TTS
    h6 = 78; y6 = y5-gap-h6
    rr(c, col2_x, y6, col2_w, h6, fill=LIGHT_BLUE)
    hdr(c, col2_x, y6, col2_w, h6, "6   TEXT-TO-SPEECH (TTS)", bg=BLUE_H)
    tx(c, col2_x+12, y6+h6-32, [
        "Question text --> tts_chunk() --> audio bytes",
        "TTS chain: Inworld AI (clone) --fail--> OpenAI TTS (nova)",
        "",
        "Audio streamed via SSE: data: {\"audio\": \"<base64>\"}",
    ])

    ad(c, col2_x+col2_w/2, y6, y6-gap)

    # 7 — Candidate Answers
    h7 = 78; y7 = y6-gap-h7
    rr(c, col2_x, y7, col2_w, h7, fill=LIGHT_GREEN)
    hdr(c, col2_x, y7, col2_w, h7, "7   CANDIDATE ANSWERS (Voice)", bg=GREEN_H)
    tx(c, col2_x+12, y7+h7-32, [
        "Browser captures microphone audio",
        "STT: Deepgram / OpenAI Whisper / Browser WebSpeech",
        "Transcribed text --> POST /submit-answer",
        "Answer stored in session conversation history",
    ])

    ad(c, col2_x+col2_w/2, y7, y7-gap)

    # 8 — Should End?
    h8 = 82; y8 = y7-gap-h8
    rr(c, col2_x, y8, col2_w, h8, fill=LIGHT_ORANGE)
    hdr(c, col2_x, y8, col2_w, h8, "8   SHOULD END?", bg=ORANGE_H)
    tx(c, col2_x+12, y8+h8-32, [
        "[END_INTERVIEW] received?  --> YES --> Evaluation",
        "Time > 1 hour (safety)?    --> YES --> Evaluation",
        "",
        "Agent rules: Min 8 main Qs | End early if weak",
        "  18+ for strong | Follow-ups don't count",
    ])

    # ═══ LOOP ARROW: Box 8 "NO" → down → far left → up → into Box 4 ═══
    loop_start_y = y8 + h8/2       # start at box8 mid-left
    loop_bottom = 65               # below everything (above summary bar)
    loop_top_y = y4 + h4 - 10      # enter near top of box 4

    # "NO" label left of box 8
    c.setFont("Helvetica-Bold", 10); c.setFillColor(ARROW_CLR)
    c.drawString(col2_x - 22, loop_start_y - 4, "NO")

    # Dashed loop: box8 left → down → far left → up → box4 left
    c.setStrokeColor(ARROW_CLR); c.setLineWidth(2.5); c.setDash(6, 3)
    p = c.beginPath()
    p.moveTo(col2_x, loop_start_y)
    p.lineTo(col2_x - 30, loop_start_y)   # short left from box 8
    p.lineTo(col2_x - 30, loop_bottom)     # down below everything
    p.lineTo(LOOP_X, loop_bottom)           # left to far margin
    p.lineTo(LOOP_X, loop_top_y)            # up to box 4 height
    p.lineTo(LM, loop_top_y)                # right into box 4
    c.drawPath(p, fill=0, stroke=1)
    c.setDash()

    # Arrowhead into box 4
    c.setFillColor(ARROW_CLR)
    _tri(c, [LM-1, loop_top_y-5, LM-1, loop_top_y+5, LM+7, loop_top_y])

    # "NEXT TURN" label (vertical along left line)
    c.saveState()
    c.setFont("Helvetica-Bold", 8); c.setFillColor(ARROW_CLR)
    c.translate(LOOP_X + 10, (loop_bottom + loop_top_y)/2)
    c.rotate(90)
    c.drawCentredString(0, 0, "NEXT TURN")
    c.restoreState()

    # ═══ YES arrow from box 8 → box 9 ═══
    c.setFont("Helvetica-Bold", 10); c.setFillColor(GREEN_CLR)
    c.drawString(col2_x + col2_w/2 + 8, y8 - 8, "YES")
    ad(c, col2_x+col2_w/2, y8, y8-gap-5, clr=GREEN_CLR)

    # ═══ BOTTOM ROW ═══

    # 9 — Evaluation
    h9 = 85; y9 = y8-gap-5-h9
    rr(c, col2_x, y9, col2_w, h9, fill=LIGHT_GREEN, stroke=DGREEN_H, sw=2)
    hdr(c, col2_x, y9, col2_w, h9, "9   EVALUATION", bg=DGREEN_H)
    tx(c, col2_x+12, y9+h9-32, [
        "Triggered when [END_INTERVIEW] + enough answers",
        "  (follow-ups excluded from answer count)",
        "",
        "Conversation --> LLM (eval prompt) --> scores:",
        "  Per-Q: score, accuracy, quality, quadrant",
        "  Overall: technical, theory, comms, behavior, grade",
    ])

    # Arrow 9 → 10
    al(c, col2_x, LM+col1_w, y9+h9/2, clr=GREEN_CLR)

    # 10 — Report & Storage
    h10 = 85; y10 = y9
    rr(c, LM, y10, col1_w, h10, fill=HexColor("#E8F5E9"), stroke=DGREEN_H, sw=2)
    hdr(c, LM, y10, col1_w, h10, "10   REPORT & STORAGE", bg=DGREEN_H)
    tx(c, LM+12, y10+h10-32, [
        "PostgreSQL:",
        "  candidates | sessions | turns | evaluations",
        "  behavioral_signals | reports | candidate_history",
        "  llm_calls (cost & token tracking)",
        "",
        "Report --> browser + LMS callback (EduSpark)",
    ])

    # ═══ DATA STORES (between box 4 bottom and box 10 top) ═══
    ds_top = y4 - 10
    ds_bot = y10 + h10 + 10
    ds_h = ds_top - ds_bot
    if ds_h > 45:
        rr(c, LM, ds_bot, col1_w, ds_h, fill=HexColor("#FDEDEC"), stroke=HexColor("#E74C3C"), sw=1.5)
        c.setFont("Helvetica-Bold", 9); c.setFillColor(HexColor("#C0392B"))
        c.drawString(LM+10, ds_bot+ds_h-15, "DATA STORES")
        c.setFont("Helvetica", 8); c.setFillColor(DARK)
        c.drawString(LM+10, ds_bot+ds_h-28, "Redis:        session cache, runtime config (fast reads)")
        c.drawString(LM+10, ds_bot+ds_h-40, "PostgreSQL: all tables (candidates, sessions, turns, ...)")

    # ═══ LOOP SUMMARY BAR ═══
    bx = 30; bw = pw-60; bh = 38; by = 15
    rr(c, bx, by, bw, bh, fill=HexColor("#FDF2E9"), stroke=ORANGE_H, sw=2)
    c.setFont("Helvetica-Bold", 10); c.setFillColor(HexColor("#D35400"))
    c.drawCentredString(pw/2, by+bh-14, "INTERVIEW LOOP")
    c.setFont("Helvetica", 9); c.setFillColor(DARK)
    c.drawCentredString(pw/2, by+bh-28,
        "4 Build Prompt --> 5 LLM Question --> 6 TTS Speaks --> "
        "7 Candidate Answers --> 8 End? --NO--> Loop | --YES--> 9 Eval --> 10 Report")

    c.save()
    print(f"PDF saved: {OUTPUT}")

if __name__ == "__main__":
    build_pdf()
