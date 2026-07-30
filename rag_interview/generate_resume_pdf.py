#!/usr/bin/env python3
"""Generate Mahesh Veeraboina's resume PDF (humanized content) using fpdf2."""
from fpdf import FPDF

NAVY = (26, 60, 110)
GRAY = (85, 85, 85)
DARK = (34, 34, 34)
LINE = (185, 198, 216)

OUTPUT = "Mahesh_Veeraboina_Resume.pdf"


class ResumePDF(FPDF):
    def __init__(self):
        super().__init__(format="A4")
        self.set_auto_page_break(auto=True, margin=14)
        self.set_margins(16, 14, 16)

    def section(self, title):
        self.ln(2.5)
        self.set_font("Helvetica", "B", 10.5)
        self.set_text_color(*NAVY)
        self.cell(0, 6, title.upper(), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*LINE)
        self.set_line_width(0.3)
        y = self.get_y()
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(1.5)
        self.set_text_color(*DARK)

    def job(self, title, company, date):
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*DARK)
        w_date = self.get_string_width(date) + 4
        self.cell(0, 5.5, f"{title} - {company}", new_x="LMARGIN")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*GRAY)
        self.cell(0, 5.5, date, align="R", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*DARK)

    def proj(self, title, sub):
        self.ln(1)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*DARK)
        self.cell(0, 5.5, title, new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "I", 8.8)
        self.set_text_color(*GRAY)
        self.cell(0, 4.5, sub, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*DARK)

    def bullets(self, items):
        self.set_font("Helvetica", "", 9.3)
        for item in items:
            x = self.l_margin + 3
            self.set_x(x)
            self.cell(4, 4.8, chr(149))  # bullet
            self.multi_cell(self.w - self.r_margin - x - 4, 4.8, item,
                            new_x="LMARGIN", new_y="NEXT")
            self.ln(0.4)

    def skill(self, label, text):
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "B", 9.3)
        self.set_text_color(*NAVY)
        w = self.get_string_width(label + " ") + 1
        self.cell(w, 4.8, label)
        self.set_font("Helvetica", "", 9.3)
        self.set_text_color(*DARK)
        self.multi_cell(self.w - self.r_margin - self.l_margin - w, 4.8, text,
                        new_x="LMARGIN", new_y="NEXT")
        self.ln(0.5)

    def edu(self, left, right):
        # Draw the right-aligned date FIRST at the current y, then let the
        # left multi_cell advance y by its own (possibly multi-line) height,
        # so a wrapped degree line can never overlap the next entry.
        y0 = self.get_y()
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*GRAY)
        self.set_xy(self.w - self.r_margin - 40, y0)
        self.cell(40, 4.8, right, align="R")
        self.set_xy(self.l_margin, y0)
        self.set_font("Helvetica", "", 9.3)
        self.set_text_color(*DARK)
        self.multi_cell(self.w - self.r_margin - self.l_margin - 42, 4.8, left,
                        new_x="LMARGIN", new_y="NEXT")
        self.ln(1)


pdf = ResumePDF()
pdf.add_page()

# ---- Header ----
pdf.set_font("Helvetica", "B", 19)
pdf.set_text_color(*NAVY)
pdf.cell(0, 9, "MAHESH VEERABOINA", new_x="LMARGIN", new_y="NEXT")
pdf.set_font("Helvetica", "", 9)
pdf.set_text_color(*GRAY)
pdf.cell(0, 4.8, "Physical Design Engineer  |  Hyderabad, Telangana, India",
         new_x="LMARGIN", new_y="NEXT")
pdf.cell(0, 4.8, "University College of Engineering, Osmania University",
         new_x="LMARGIN", new_y="NEXT")
pdf.set_draw_color(*NAVY)
pdf.set_line_width(0.7)
y = pdf.get_y() + 1.5
pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
pdf.ln(3)

# ---- Profile Summary ----
pdf.section("Profile Summary")
pdf.set_font("Helvetica", "", 9.3)
pdf.multi_cell(0, 4.8,
    "Physical Design Engineer working on 28nm RTL-to-GDSII implementation. I've taken blocks from "
    "floorplan through DRC/LVS signoff - the largest being a 350K-cell SoC block with 24 macros and "
    "5 clock domains. Comfortable automating PnR flows with TCL and Makefiles on Cadence "
    "Innovus/Genus, and currently building depth in low-power design and power integrity. Looking "
    "to apply this in high-performance mixed-signal IP work.",
    new_x="LMARGIN", new_y="NEXT")

# ---- Work Experience ----
pdf.section("Work Experience")

pdf.job("Physical Design Engineer", "SemiconOS", "May 2026 - Present")
pdf.bullets([
    "Went through intensive hands-on training on the full RTL-to-GDSII flow (Cadence Innovus + "
    "Genus, 28nm): floorplanning, power grid, placement, CTS, routing, timing closure, DRC fixes.",
    "Independently completed three block-level projects - RPTOP (69K gates), ALU (428 gates), and "
    "Tile (302K cells, 18 macros) - each delivered as DRC-clean GDSII.",
    "Scripted the repetitive parts (constraint loading, placement runs, post-route reports) in "
    "TCL/TCSH so each iteration takes less manual work.",
])

pdf.job("Physical Design Engineer Intern", "RiseTime Semiconductors, Hyderabad",
        "June 2025 - May 2026")
pdf.bullets([
    "Automated the end-to-end PnR flow (floorplan to signoff) with TCL scripts and Makefiles, so "
    "runs are repeatable across design blocks instead of being redone by hand each time.",
    "Owned the physical implementation of the Iguana SoC block - ~350K standard cells, 24 hard "
    "macros, 5 independent clock domains - covering floorplan architecture, macro placement, "
    "multi-clock CTS, timing convergence, and congestion cleanup.",
    "Wrote reusable TCL utilities for congestion analysis, timing report parsing, and design-rule "
    "checks - these cut down iteration time noticeably during closure.",
    "Worked with senior engineers on power grid strategy and placement guidelines for blocks with "
    "multiple power domains and tight PPA targets.",
    "Handled DRC/LVS signoff-readiness checks for large block implementations.",
])

# ---- Technical Skills ----
pdf.section("Technical Skills")
pdf.skill("Physical Design Flow:",
          "Floorplanning, Power Planning & Power Grid Design, Placement, Clock Tree Synthesis "
          "(CTS), Routing, Timing Closure, DRC/LVS Signoff - full RTL-to-GDSII on 28nm")
pdf.skill("EDA Tools:",
          "Cadence Innovus (Place & Route), Cadence Genus (Synthesis); basic exposure to Synopsys "
          "tools and formal equivalence verification (Formality)")
pdf.skill("Timing & Verification:",
          "Static Timing Analysis (STA), setup/hold violation fixing, clock domain analysis, "
          "physical verification (DRC, LVS, antenna checks)")
pdf.skill("Design Knowledge:",
          "CMOS fundamentals, PPA trade-offs, multi-clock domain design, hard macro placement, "
          "congestion resolution, low-power concepts and multiple power domain awareness")
pdf.skill("Scripting & Automation:",
          "TCL, TCSH, Makefiles - flow automation, report generation, constraint management, "
          "DRC checking scripts")
pdf.skill("HDL:", "Basic Verilog HDL")

# ---- Projects ----
pdf.section("Projects")

pdf.proj("Iguana - SoC Block-Level Physical Implementation",
         "28nm Physical Design (Place & Route)  |  Cadence Innovus")
pdf.bullets([
    "The most complex block I've worked on: ~350K cells, 24 hard macros, 5 clock domains - the "
    "floorplan and power grid had to account for all five domains from day one.",
    "Responsible for macro placement, floorplan definition, and multi-clock CTS to meet skew and "
    "insertion-delay targets across all domains.",
    "Built the Makefile/TCL automation that runs the full flow reproducibly; currently driving "
    "timing convergence and congestion fixes in high-utilization regions ahead of DRC/LVS signoff.",
])

pdf.proj("Tile - Block-Level Physical Implementation",
         "28nm RTL to GDSII  |  Cadence Innovus")
pdf.bullets([
    "Largest block I've closed independently: 302,511 cells, 18 hard macros, 1.7ns clock.",
    "Ran the full flow - hierarchical floorplan, macro placement, multi-layer power grid, "
    "congestion-driven placement, CTS with skew balancing, detailed routing - through to "
    "DRC-clean GDSII.",
    "Closed timing at 1.7ns by working through setup/hold violations with placement optimization "
    "and clock-tree tuning; automated iterative runs with TCL/Makefiles to keep turnaround short.",
])

pdf.proj("RPTOP - Block-Level Implementation",
         "28nm RTL to GDSII  |  Cadence Innovus & Genus")
pdf.bullets([
    "69K-gate block, 6 hard macros, one 1.8ns primary clock plus a virtual clock - did the "
    "floorplan, power rings/straps, CTS, and detailed routing.",
    "Fixed routing congestion by tuning placement constraints, cleared all setup/hold violations, "
    "and delivered DRC-clean GDSII; constraint loading and reporting scripted in TCL.",
])

pdf.proj("ALU - Logic Synthesis",
         "28nm RTL to Gate-Level Netlist  |  Cadence Genus")
pdf.bullets([
    "Synthesized a 428-gate ALU with primary (2ns), generated, and virtual clocks; applied SDC "
    "constraints and optimized for timing and area.",
    "Verified QoR with Genus reports and confirmed formal equivalence of the synthesized netlist.",
])

# ---- Education ----
pdf.section("Education")
pdf.edu("B.E. in Electrical and Electronics Engineering - University College of Engineering, "
        "Osmania University", "May 2025  |  78%")
pdf.edu("Intermediate (MPC) - Narayana Junior College, Hyderabad", "March 2021  |  97%")

# ---- Achievements ----
pdf.section("Academic Achievements")
pdf.bullets([
    "Awarded the Foundation for Excellence Scholarship - a merit-based national scholarship for "
    "academic excellence (2021-2022).",
])

pdf.output(OUTPUT)
print(f"Generated: {OUTPUT}")
