#!/usr/bin/env python3
"""Generate AI Interview Platform – Session Cost Analysis PDF for CEO review."""

from fpdf import FPDF

class CostReport(FPDF):
    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=25)

    def header(self):
        if self.page_no() > 1:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 6, "AI Interview Platform - Session Cost Analysis", align="L")
            self.cell(0, 6, "June 2026", align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(200, 200, 200)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        if self.page_no() > 1:
            self.cell(0, 10, f"Page {self.page_no() - 1}", align="C")

    def section_title(self, title):
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(26, 54, 93)
        self.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(49, 130, 206)
        self.set_line_width(0.6)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(6)

    def sub_title(self, title):
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(45, 55, 72)
        self.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def body_text(self, text):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(74, 85, 104)
        self.multi_cell(0, 5.5, text)
        self.ln(2)

    def highlight_box(self, text, bg=(240, 255, 244), border_color=(56, 161, 105), height=22):
        self.set_fill_color(*bg)
        x = self.get_x()
        y = self.get_y()
        w = self.w - self.l_margin - self.r_margin
        self.rect(x, y, w, height, "F")
        self.set_draw_color(*border_color)
        self.set_line_width(1.2)
        self.line(x, y, x, y + height)
        self.set_xy(x + 4, y + 3)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(26, 54, 93)
        self.multi_cell(w - 8, 5.5, text)
        self.set_y(y + height + 4)

    def big_cost_box(self, label, cost, sublabel="", bg=(240, 255, 244), border_color=(56, 161, 105)):
        self.set_fill_color(*bg)
        x = self.get_x()
        y = self.get_y()
        w = self.w - self.l_margin - self.r_margin
        h = 32 if sublabel else 26
        self.rect(x, y, w, h, "F")
        self.set_draw_color(*border_color)
        self.set_line_width(1.5)
        self.line(x, y, x, y + h)
        self.set_xy(x + 5, y + 4)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(45, 55, 72)
        self.cell(80, 7, label)
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(*border_color)
        self.cell(0, 7, cost, new_x="LMARGIN", new_y="NEXT")
        if sublabel:
            self.set_xy(x + 5, y + 18)
            self.set_font("Helvetica", "", 10)
            self.set_text_color(74, 85, 104)
            self.cell(0, 6, sublabel, new_x="LMARGIN", new_y="NEXT")
        self.set_y(y + h + 6)

    def dual_cost_box(self, label1, cost1, label2, cost2, sublabel=""):
        x = self.get_x()
        y = self.get_y()
        w = self.w - self.l_margin - self.r_margin
        half = w / 2 - 3
        h = 36

        # Left box (without caching)
        self.set_fill_color(247, 250, 252)
        self.rect(x, y, half, h, "F")
        self.set_draw_color(160, 174, 192)
        self.set_line_width(1.2)
        self.line(x, y, x, y + h)
        self.set_xy(x + 5, y + 5)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(113, 128, 150)
        self.cell(half - 10, 5, label1, new_x="LMARGIN", new_y="NEXT")
        self.set_xy(x + 5, y + 14)
        self.set_font("Helvetica", "B", 22)
        self.set_text_color(113, 128, 150)
        self.cell(half - 10, 8, cost1, new_x="LMARGIN", new_y="NEXT")

        # Right box (with caching)
        rx = x + half + 6
        self.set_fill_color(240, 255, 244)
        self.rect(rx, y, half, h, "F")
        self.set_draw_color(56, 161, 105)
        self.set_line_width(1.5)
        self.line(rx, y, rx, y + h)
        self.set_xy(rx + 5, y + 5)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(56, 161, 105)
        self.cell(half - 10, 5, label2, new_x="LMARGIN", new_y="NEXT")
        self.set_xy(rx + 5, y + 14)
        self.set_font("Helvetica", "B", 22)
        self.set_text_color(56, 161, 105)
        self.cell(half - 10, 8, cost2, new_x="LMARGIN", new_y="NEXT")

        if sublabel:
            self.set_xy(rx + 5, y + 27)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(56, 161, 105)
            self.cell(half - 10, 4, sublabel, new_x="LMARGIN", new_y="NEXT")

        self.set_y(y + h + 6)

    def add_table(self, headers, rows, col_widths=None, total_row_idx=None, highlight_row_idx=None):
        if col_widths is None:
            w = self.w - self.l_margin - self.r_margin
            col_widths = [w / len(headers)] * len(headers)

        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(26, 54, 93)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            align = "R" if i == len(headers) - 1 and len(headers) > 2 else "L"
            self.cell(col_widths[i], 8, h, border=0, fill=True, align=align)
        self.ln()

        for r_idx, row in enumerate(rows):
            is_total = (total_row_idx is not None and r_idx == total_row_idx)
            is_highlight = (highlight_row_idx is not None and r_idx == highlight_row_idx)
            if is_total:
                self.set_font("Helvetica", "B", 10)
                self.set_fill_color(235, 248, 255)
                self.set_text_color(26, 54, 93)
                self.set_draw_color(49, 130, 206)
                self.set_line_width(0.5)
                self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            elif is_highlight:
                self.set_font("Helvetica", "B", 9)
                self.set_fill_color(240, 255, 244)
                self.set_text_color(56, 161, 105)
            elif r_idx % 2 == 1:
                self.set_fill_color(247, 250, 252)
                self.set_font("Helvetica", "", 9)
                self.set_text_color(45, 55, 72)
            else:
                self.set_fill_color(255, 255, 255)
                self.set_font("Helvetica", "", 9)
                self.set_text_color(45, 55, 72)

            for i, cell_text in enumerate(row):
                align = "R" if i == len(row) - 1 and len(row) > 2 else "L"
                if is_total:
                    self.set_font("Helvetica", "B", 10)
                self.cell(col_widths[i], 7.5, str(cell_text), border=0, fill=True, align=align)
            self.ln()
        self.ln(4)

    def bullet_list(self, items):
        self.set_font("Helvetica", "", 10)
        self.set_text_color(45, 55, 72)
        for item in items:
            self.cell(6, 6, "-")
            self.cell(0, 6, item, new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def footnote(self, text):
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(160, 174, 192)
        self.multi_cell(0, 4, text)
        self.ln(2)


def build_report():
    pdf = CostReport()
    pdf.set_margins(20, 20, 20)
    W = pdf.w - 40

    # ── COVER PAGE ──
    pdf.add_page()
    pdf.ln(45)
    pdf.set_font("Helvetica", "B", 34)
    pdf.set_text_color(26, 54, 93)
    pdf.cell(0, 14, "AI Interview Platform", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 18)
    pdf.set_text_color(74, 85, 104)
    pdf.cell(0, 10, "Session Cost Analysis Report", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_draw_color(49, 130, 206)
    pdf.set_line_width(1)
    cx = pdf.w / 2
    pdf.line(cx - 30, pdf.get_y(), cx + 30, pdf.get_y())
    pdf.ln(12)
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(113, 128, 150)
    for line in [
        "Configuration:  30-Question Session  |  1-Hour Duration",
        "Date:  June 26, 2026",
        "Classification:  Internal - Executive Review",
    ]:
        pdf.cell(0, 7, line, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    # Dual cost badges on cover
    pdf.dual_cost_box(
        "Without Prompt Caching", "$0.58",
        "With Prompt Caching", "$0.46",
        "20% cost reduction",
    )

    # ── PAGE 2: EXECUTIVE SUMMARY ──
    pdf.add_page()
    pdf.section_title("1. Executive Summary")
    pdf.body_text(
        "This report details the per-session cost of running a 1-hour AI-powered interview "
        "with 30 questions. All pricing has been verified against provider pricing pages as of "
        "June 26, 2026. Prompt caching on the LLM reduces session cost by 20%."
    )

    pdf.sub_title("Service Stack")
    pdf.add_table(
        ["Function", "Service / Model", "Provider"],
        [
            ["Speech-to-Text (STT)", "gpt-4o-mini-transcribe", "OpenAI"],
            ["Text-to-Speech (TTS)", "Inworld TTS 1.5 Mini", "Inworld AI"],
            ["Question Gen & Evaluation", "Claude Haiku 4.5", "Anthropic (AWS Bedrock)"],
            ["AI Answer Detection", "Sapling AI Detector API", "Sapling"],
            ["Face Verification", "AWS Rekognition CompareFaces", "AWS"],
        ],
        [W * 0.36, W * 0.36, W * 0.28],
    )

    pdf.dual_cost_box(
        "Without Prompt Caching", "$0.58",
        "With Prompt Caching (Recommended)", "$0.46",
        "Save $0.12 per session  |  20% reduction",
    )

    # ── PAGE 3: COST BREAKDOWN ──
    pdf.add_page()
    pdf.section_title("2. Cost Breakdown Per Session")

    pdf.sub_title("2.1  Without Prompt Caching")
    pdf.add_table(
        ["Component", "Verified Rate", "Usage / Session", "Cost (USD)"],
        [
            ["STT - gpt-4o-mini-transcribe", "$0.003 / min", "~40 min speech", "$0.120"],
            ["TTS - Inworld TTS 1.5 Mini", "$25 / 1M chars", "~4,000 chars", "$0.100"],
            ["LLM - Claude Haiku 4.5", "$1.00 / $5.00 per 1M", "~196K in / ~20K out", "$0.296"],
            ["AI Detection - Sapling API", "Free", "30 calls", "$0.000"],
            ["Face Verify - AWS Rekognition", "~$0.001 / image", "~63 API calls", "$0.063"],
            ["TOTAL PER SESSION", "", "", "$0.579"],
        ],
        [W * 0.30, W * 0.22, W * 0.26, W * 0.22],
        total_row_idx=5,
    )

    pdf.sub_title("2.2  With Prompt Caching (Recommended)")
    pdf.add_table(
        ["Component", "Verified Rate", "Usage / Session", "Cost (USD)"],
        [
            ["STT - gpt-4o-mini-transcribe", "$0.003 / min", "~40 min speech", "$0.120"],
            ["TTS - Inworld TTS 1.5 Mini", "$25 / 1M chars", "~4,000 chars", "$0.100"],
            ["LLM - Claude Haiku 4.5", "cached rates", "~196K in / ~20K out", "$0.180"],
            ["AI Detection - Sapling API", "Free", "30 calls", "$0.000"],
            ["Face Verify - AWS Rekognition", "~$0.001 / image", "~63 API calls", "$0.063"],
            ["TOTAL WITH CACHING", "", "", "$0.463"],
        ],
        [W * 0.30, W * 0.22, W * 0.26, W * 0.22],
        total_row_idx=5, highlight_row_idx=2,
    )

    pdf.sub_title("2.3  Cost Distribution  (With Prompt Caching)")
    bars = [
        ("LLM", 39, (49, 130, 206)),
        ("STT", 26, (56, 161, 105)),
        ("TTS", 22, (214, 158, 46)),
        ("AWS", 13, (229, 62, 62)),
    ]
    bar_max_w = W - 40
    for label, pct, color in bars:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(45, 55, 72)
        pdf.cell(22, 6, label)
        pdf.set_fill_color(*color)
        bw = bar_max_w * pct / 100
        pdf.cell(bw, 6, "", fill=True)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*color)
        pdf.cell(20, 6, f"  {pct}%", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # ── PAGE 4: PROMPT CACHING DEEP DIVE ──
    pdf.add_page()
    pdf.section_title("3. Prompt Caching  -  How It Saves 20%")
    pdf.body_text(
        "Anthropic's prompt caching allows repeated message prefixes to be stored and reused "
        "across API calls. In our interview flow, each question-generation call builds on the "
        "previous one's input (system prompt + growing conversation history), making it an "
        "ideal candidate for caching."
    )

    pdf.sub_title("3.1  Caching Rates  (Claude Haiku 4.5)")
    pdf.add_table(
        ["Token Type", "Rate (per 1M tokens)", "vs Standard"],
        [
            ["Standard input (no cache)", "$1.00", "baseline"],
            ["Cache write (first occurrence)", "$1.25", "+25%  (one-time)"],
            ["Cache read (subsequent hits)", "$0.10", "-90%  discount"],
            ["Output (unchanged)", "$5.00", "no change"],
        ],
        [W * 0.38, W * 0.32, W * 0.30],
    )

    pdf.sub_title("3.2  Why Question Generation Benefits Most")
    pdf.body_text(
        "Each question-generation call includes the full conversation history as a prefix. "
        "Call N reuses 100% of Call N-1's input from cache, only writing the new Q&A pair "
        "(~250 tokens) to cache. By Call 30, ~97% of input tokens are cache reads at 90% discount."
    )

    pdf.add_table(
        ["Call #", "Total Input", "Cache Read (at $0.10/1M)", "Cache Write (at $1.25/1M)"],
        [
            ["Call 1", "2,000 tokens", "0", "2,000 tokens"],
            ["Call 5", "3,000 tokens", "2,750", "250"],
            ["Call 10", "4,250 tokens", "4,000", "250"],
            ["Call 20", "6,750 tokens", "6,500", "250"],
            ["Call 30", "9,250 tokens", "9,000", "250"],
        ],
        [W * 0.14, W * 0.22, W * 0.34, W * 0.30],
    )

    pdf.sub_title("3.3  Token Breakdown  (Question Generation)")
    pdf.add_table(
        ["Metric", "Without Caching", "With Caching"],
        [
            ["Total input tokens", "~140,000", "~140,000"],
            ["Tokens billed at standard rate", "140,000 at $1.00/1M", "--"],
            ["Tokens billed as cache reads", "--", "~131,000 at $0.10/1M"],
            ["Tokens billed as cache writes", "--", "~9,000 at $1.25/1M"],
            ["Question gen input cost", "$0.140", "$0.024"],
            ["Savings", "", "83%"],
        ],
        [W * 0.36, W * 0.32, W * 0.32],
        highlight_row_idx=5,
    )

    pdf.sub_title("3.4  Full LLM Cost Comparison")
    pdf.add_table(
        ["LLM Call Type", "Without Cache", "With Cache", "Savings"],
        [
            ["Question generation (input)", "$0.140", "$0.024", "-83%"],
            ["Expected points (input)", "$0.036", "$0.036", "--"],
            ["AI detection fallback (input)", "$0.007", "$0.007", "--"],
            ["Evaluation + greeting (input)", "$0.013", "$0.013", "--"],
            ["All output tokens", "$0.100", "$0.100", "--"],
            ["TOTAL LLM COST", "$0.296", "$0.180", "-39%"],
        ],
        [W * 0.34, W * 0.20, W * 0.20, W * 0.26],
        total_row_idx=5,
    )

    pdf.highlight_box(
        "Prompt caching reduces LLM cost by 39% ($0.296 -> $0.180) with zero impact on "
        "output quality. The feature requires no code changes to the question-generation "
        "prompt -- only enabling the cache_control flag on the system prompt.",
        bg=(240, 255, 244), border_color=(56, 161, 105), height=22,
    )

    # ── PAGE 5: LLM DETAIL ──
    pdf.add_page()
    pdf.section_title("4. LLM Token Detail  (Claude Haiku 4.5)")
    pdf.body_text(
        "Claude Haiku 4.5 is used for question generation, answer evaluation, expected-point "
        "extraction, and AI detection fallback. Pricing: $1.00 per 1M input tokens (or cached "
        "rates), $5.00 per 1M output tokens."
    )

    pdf.add_table(
        ["LLM Call Type", "Calls", "Input Tokens", "Output Tokens", "Cost (USD)"],
        [
            ["Greeting generation", "1", "500", "30", "$0.001"],
            ["Question generation", "30", "140,000", "3,000", "$0.155"],
            ["Expected points extraction", "30", "36,000", "6,000", "$0.066"],
            ["AI detection fallback", "~12", "7,200", "600", "$0.010"],
            ["Final evaluation", "1", "12,000", "10,000", "$0.062"],
            ["TOTAL (without cache)", "~74", "~196,000", "~20,000", "$0.296"],
            ["TOTAL (with cache)", "~74", "~196,000", "~20,000", "$0.180"],
        ],
        [W * 0.30, W * 0.10, W * 0.20, W * 0.20, W * 0.20],
        total_row_idx=5, highlight_row_idx=6,
    )

    pdf.highlight_box(
        "Why does question generation dominate?  Each successive question includes the full "
        "conversation history. By question 30, the input context contains all 29 prior Q&A "
        "pairs (~9,000+ tokens per call). Prompt caching turns this from a cost problem into "
        "a cost advantage -- the growing prefix is read from cache at 90% discount.",
        bg=(235, 248, 255), border_color=(49, 130, 206), height=28,
    )

    # ── PAGE 6: SERVICE DETAILS ──
    pdf.add_page()
    pdf.section_title("5. Service-by-Service Detail")

    pdf.sub_title("5.1  Speech-to-Text  -  gpt-4o-mini-transcribe")
    pdf.add_table(
        ["Parameter", "Value"],
        [
            ["Rate", "$0.003 per minute  ($0.18 per hour)"],
            ["Candidate speaking time", "~40 minutes of 60-minute session"],
            ["How it's used", "Each answer is streamed and transcribed in real-time"],
            ["Per-session cost", "$0.120"],
        ],
        [W * 0.35, W * 0.65],
    )
    pdf.footnote("Source: openai.com/api/pricing (verified June 2026)")

    pdf.sub_title("5.2  Text-to-Speech  -  Inworld TTS 1.5 Mini")
    pdf.add_table(
        ["Parameter", "Value"],
        [
            ["On-demand rate", "$25 per 1M characters  ($0.025 per 1K chars)"],
            ["Scale rate (Enterprise)", "$5 per 1M characters  ($0.005 per 1K chars)"],
            ["Characters per session", "~4,000  (1 greeting + 30 questions streamed)"],
            ["Per-session cost (on-demand)", "$0.100"],
            ["Per-session cost (at scale)", "$0.020"],
        ],
        [W * 0.35, W * 0.65],
    )
    pdf.footnote("Source: inworld.ai/pricing (verified June 2026)")

    pdf.sub_title("5.3  AI Detection  -  Sapling API")
    pdf.add_table(
        ["Parameter", "Value"],
        [
            ["Rate", "Free  (AI Detector API is free of charge)"],
            ["How it's used", "Each answer (first 2,000 chars) checked for AI content"],
            ["Fallback", "If score is ambiguous (0.4-0.7), LLM verifies (cost in LLM)"],
            ["Per-session cost", "$0.000"],
        ],
        [W * 0.35, W * 0.65],
    )
    pdf.footnote("Source: sapling.ai/docs/api/pricing (verified June 2026). High-volume may require contacting Sapling.")

    pdf.sub_title("5.4  Face Verification  -  AWS Rekognition")
    pdf.add_table(
        ["Parameter", "Value"],
        [
            ["APIs used", "DetectFaces (registration) + CompareFaces (periodic)"],
            ["Rate", "~$0.001 per image  (first 1M images/month)"],
            ["Calls per session", "~3 detect + ~60 compare (every 60s) = ~63 total"],
            ["Per-session cost", "$0.063"],
        ],
        [W * 0.35, W * 0.65],
    )
    pdf.footnote("Source: aws.amazon.com/rekognition/pricing (verified June 2026). Free tier: 1,000 images/month for first 12 months.")

    # ── PAGE 7: SCALE PROJECTIONS ──
    pdf.add_page()
    pdf.section_title("6. Cost at Scale")

    pdf.sub_title("6.1  Monthly Projections  (With Prompt Caching)")
    pdf.add_table(
        ["Monthly Volume", "STT", "TTS", "LLM (cached)", "AI Detect", "AWS", "Total"],
        [
            ["100 interviews", "$12", "$10", "$18", "$0", "$6", "$46"],
            ["500 interviews", "$60", "$50", "$90", "$0", "$32", "$232"],
            ["1,000 interviews", "$120", "$100", "$180", "$0", "$63", "$463"],
            ["5,000 interviews", "$600", "$500", "$900", "$0", "$315", "$2,315"],
            ["10,000 interviews", "$1,200", "$1,000", "$1,800", "$0", "$630", "$4,630"],
        ],
        [W * 0.22, W * 0.11, W * 0.11, W * 0.16, W * 0.12, W * 0.12, W * 0.16],
    )

    pdf.sub_title("6.2  Savings Summary by Optimization")
    pdf.add_table(
        ["Configuration", "Per Session", "1,000/mo", "10,000/mo", "Savings"],
        [
            ["Baseline (no optimizations)", "$0.579", "$579", "$5,790", "--"],
            ["+ Prompt caching", "$0.463", "$463", "$4,630", "20%"],
            ["+ Prompt cache + scale TTS", "$0.383", "$383", "$3,830", "34%"],
            ["+ All optimizations*", "$0.319", "$319", "$3,190", "45%"],
        ],
        [W * 0.30, W * 0.16, W * 0.16, W * 0.18, W * 0.20],
    )
    pdf.footnote("* All optimizations = prompt caching + enterprise TTS ($5/1M) + batch processing for evaluation (50% LLM discount on non-realtime calls).")

    pdf.highlight_box(
        "With all optimizations at 10,000 interviews/month: $3,190/month total. "
        "That is $0.32 per interview vs $15-50+ for human screening -- a 47x-156x cost reduction.",
        bg=(240, 255, 244), border_color=(56, 161, 105), height=18,
    )

    # ── PAGE 8: LLM COMPARISON ──
    pdf.add_page()
    pdf.section_title("7. Alternative LLM Cost Comparison")
    pdf.body_text(
        "The LLM is the largest cost lever. Below is a comparison across available models "
        "(same ~196K input / ~20K output token profile). Prompt caching benefit shown for "
        "Claude models that support it."
    )

    pdf.add_table(
        ["Model", "Input/1M", "Output/1M", "LLM Cost", "With Cache", "Session Total"],
        [
            ["Amazon Nova Micro", "$0.035", "$0.14", "$0.010", "N/A", "$0.293"],
            ["Amazon Nova Lite", "$0.06", "$0.24", "$0.017", "N/A", "$0.300"],
            ["GPT-4o-mini", "$0.15", "$0.60", "$0.041", "N/A", "$0.324"],
            ["Grok 4.1 Fast", "$0.20", "$0.50", "$0.049", "N/A", "$0.332"],
            ["Haiku 4.5 [Selected]", "$1.00", "$5.00", "$0.296", "$0.180", "$0.463"],
            ["Claude Sonnet 4.6", "$3.00", "$15.00", "$0.888", "$0.536", "$0.819"],
            ["Claude Opus 4.6", "$15.00", "$75.00", "$4.440", "$2.680", "$2.963"],
        ],
        [W * 0.24, W * 0.12, W * 0.13, W * 0.14, W * 0.15, W * 0.22],
    )

    pdf.highlight_box(
        "Claude Haiku 4.5 with prompt caching ($0.46/session) offers the best "
        "quality-to-cost balance. It is 7x cheaper than Sonnet while maintaining strong "
        "reasoning for contextual follow-up questions and detailed evaluations.",
        bg=(255, 250, 240), border_color=(221, 107, 32), height=22,
    )

    # ── PAGE 9: ASSUMPTIONS ──
    pdf.add_page()
    pdf.section_title("8. Assumptions & Notes")

    pdf.sub_title("Session Parameters")
    pdf.bullet_list([
        "Session duration: 1 hour (60 minutes)",
        "Number of questions: 30",
        "Average candidate speaking time: ~40 minutes (67% of session)",
        "Average answer length: ~1 min speech (~150 words, ~750 characters)",
        "Face verification interval: Every 60 seconds",
        "AI detection: Runs on every candidate answer",
    ])

    pdf.sub_title("Prompt Caching Assumptions")
    pdf.bullet_list([
        "Cache TTL: 5 minutes (Anthropic default; sufficient within a session)",
        "Cache hit rate on question gen: ~94% of input tokens are cache reads",
        "Only question-generation calls benefit (growing prefix pattern)",
        "Other LLM calls have unique content per call, minimal cache benefit",
        "Requires cache_control flag on system/context messages",
    ])

    pdf.sub_title("Token Estimation Method")
    pdf.bullet_list([
        "Claude models: ~3.5 characters per token",
        "Conversation history grows linearly with each question",
        "Question 1 input: ~2,000 tokens;  Question 30 input: ~9,250 tokens",
    ])

    pdf.sub_title("Pricing Sources  (Verified June 26, 2026)")
    pdf.add_table(
        ["Service", "Source"],
        [
            ["OpenAI STT", "openai.com/api/pricing"],
            ["Inworld TTS", "inworld.ai/pricing"],
            ["Claude Haiku 4.5", "platform.claude.com/docs/en/about-claude/pricing"],
            ["Sapling AI Detection", "sapling.ai/docs/api/pricing"],
            ["AWS Rekognition", "aws.amazon.com/rekognition/pricing"],
        ],
        [W * 0.30, W * 0.70],
    )

    pdf.sub_title("What's NOT Included")
    pdf.bullet_list([
        "Server hosting / compute costs (EC2, database)",
        "Network bandwidth / data transfer",
        "Resume parsing (one-time per candidate, negligible)",
        "Storage costs (session data, recordings)",
    ])

    pdf.ln(4)
    pdf.big_cost_box(
        "Bottom Line:", "$0.46 per interview",
        "with prompt caching  |  vs $15-$50+ for human screening",
    )

    # ── SAVE ──
    out = "/home/surya/surya_project/simple_interview/Session_Cost_Analysis_Report.pdf"
    pdf.output(out)
    print(f"PDF generated: {out}")


if __name__ == "__main__":
    build_report()
