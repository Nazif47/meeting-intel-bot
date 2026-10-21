"""
PDF report generation using fpdf2 — one PDF per department.

Each PDF contains:
  1. Header         : Department name + report date on every page.
  2. Sheet link     : Clickable link to the Google Sheets database.
  3. Per-Agent sections (one per unique "from" value):
       a) Stylized agent header with name & extension.
       b) Mini table  : Calls Attempted | Actual Conversations (this agent only).
       c) "Conversations" sub-header → linear call blocks (is_actual_conversation=True).
       d) "Attempts"   sub-header → linear call blocks (is_actual_conversation=False).
  4. Transcript pages : One page per actual conversation, appended at the end.
       "Full Transcript" links in call blocks jump here.
"""
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

import config

# ── Colour palette ─────────────────────────────────────────────────────────────
_C_TITLE   = (44,  62,  80)   # dark blue-grey — headings
_C_LABEL   = (80,  80,  80)   # mid-grey       — labels
_C_BODY    = (30,  30,  30)   # near-black     — body text
_C_AI      = (52,  73,  94)   # deep navy      — AI content
_C_LINK    = (41, 128, 185)   # blue           — hyperlinks
_C_RULE    = (210, 210, 210)  # light grey     — separator lines
_C_TH_BG   = (44,  62,  80)   # table header background
_C_TH_FG   = (255, 255, 255)  # table header text
_C_AGENT   = (236, 240, 241)  # agent header background (very light grey)
_C_EVEN    = (245, 247, 250)  # alternating row tint
_C_POS     = (39, 174,  96)   # green  — Positive / Conversations
_C_NEU     = (127, 140, 141)  # grey   — Neutral
_C_NEG     = (192,  57,  43)  # red    — Negative / Angry / Attempts
_C_BLACK   = (0,   0,   0)    # reset colour

# Effective page width: A4 (210 mm) − 15 mm left − 15 mm right = 180 mm
_PW = 180

# Column widths for the global summary table (must sum to _PW)
_SUMMARY_COLS = (96, 42, 42)   # Agent | Attempted | Conversations

# Column widths for the per-agent mini table (must sum to _PW)
_MINI_COLS = (90, 45, 45)   # Agent name | Attempted | Conversations

_SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{config.SPREADSHEET_ID}/edit#gid=0"
)

# ── Unicode → Latin-1 sanitiser ────────────────────────────────────────────────

_UNICODE_MAP = str.maketrans({
    "→": "->",   # →  right arrow
    "│": "|",    # │  box-drawing vertical
    "•": "-",    # •  bullet
    "—": "--",   # —  em dash
    "–": "-",    # –  en dash
    "“": '"',  # "  left double quote
    "”": '"',  # "  right double quote
    "‘": "'",  # '  left single quote
    "’": "'",  # '  right single quote
    "…": "...",  # …  ellipsis
    " ": " ",  # non-breaking space
})


def _s(text: str) -> str:
    """Sanitise text for Helvetica (Latin-1). Known substitutions first,
    then encode/decode to replace any remaining out-of-range characters."""
    cleaned = str(text).translate(_UNICODE_MAP)
    return cleaned.encode("latin-1", errors="replace").decode("latin-1")


# ── FPDF subclass ──────────────────────────────────────────────────────────────

class _ReportPDF(FPDF):

    def __init__(self, dept_name: str, report_date: str, **kwargs):
        super().__init__(orientation="P", unit="mm", format="A4", **kwargs)
        self._dept_name   = _s(dept_name)
        self._report_date = _s(report_date)

    def header(self):
        self._fmt(_C_TITLE, "B", 12)
        self.cell(0, 7, "Call Intelligence Report",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self._fmt(_C_LABEL, "", 9)
        self.cell(0, 5, f"{self._dept_name}   |   {self._report_date}",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
        self.ln(2)
        self._rule()
        self.ln(3)

    def footer(self):
        self.set_y(-13)
        self._fmt(_C_LABEL, "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")

    def _fmt(self, color: tuple, style: str = "", size: int = 10) -> None:
        self.set_font("Helvetica", style, size)
        self.set_text_color(*color)

    def _rule(self) -> None:
        self.set_draw_color(*_C_RULE)
        self.line(self.l_margin, self.get_y(),
                  self.w - self.r_margin, self.get_y())


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_department_pdfs(
    dept_groups: dict[str, list[dict]],
    report_date: str,
) -> dict[str, str]:
    """
    Generate one PDF per department entry in *dept_groups*.
    Returns {dept_name: temp_file_path}.
    The caller is responsible for deleting the files after sending.
    """
    return {
        dept: _generate_one(dept, report_date, records)
        for dept, records in dept_groups.items()
        if records
    }


# ── Per-department PDF builder ─────────────────────────────────────────────────

def _generate_one(dept_name: str, report_date: str, records: list[dict]) -> str:
    pdf = _ReportPDF(dept_name=dept_name, report_date=report_date)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(left=15, top=15, right=15)
    pdf.add_page()

    # ── Sheet hyperlink ────────────────────────────────────────────────────
    pdf._fmt(_C_LABEL, "", 9)
    pdf.cell(0, 5, "View Full Database in Google Sheets  ->",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf._fmt(_C_LINK, "", 9)
    pdf.cell(0, 5, _s(_SHEET_URL),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT, link=_SHEET_URL)
    pdf.ln(6)

    # ── Group records by date (sorted chronologically) ─────────────────────
    by_date: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        by_date[rec.get("date", "Unknown")].append(rec)

    # Transcript queue: (link_id, rec) — populated as we render call blocks,
    # consumed at the very end so add_page() always precedes set_link().
    transcript_queue: list[tuple[int, dict]] = []

    for date_str in sorted(by_date.keys()):
        date_records = by_date[date_str]

        # ── Date section header ────────────────────────────────────────────
        _add_date_header(pdf, date_str)

        # ── Per-date global summary (all agents who worked this date) ──────
        _add_global_summary_table(pdf, date_records)
        pdf.ln(6)

        # ── Per-agent sections for this date ───────────────────────────────
        by_agent: dict[str, list[dict]] = defaultdict(list)
        for rec in date_records:
            by_agent[rec.get("from", "Unknown")].append(rec)

        for agent, agent_records in by_agent.items():
            _add_agent_header(pdf, agent)
            _add_agent_mini_table(pdf, agent, agent_records)
            pdf.ln(4)

            # Meet records are always actual conversations regardless of AI flag.
            convos   = [r for r in agent_records if r.get("source") == "meet" or r.get("is_actual_conversation", True)]
            attempts = [r for r in agent_records if r.get("source") != "meet" and not r.get("is_actual_conversation", True)]

            if convos:
                _add_section_subheader(pdf, "Conversations")
                for rec in convos:
                    link_id: int | None = None
                    if (rec.get("full_transcript") or "").strip():
                        link_id = pdf.add_link()
                        transcript_queue.append((link_id, rec))
                    _add_call_block(pdf, rec, link_id)
                    pdf.ln(3)

            if attempts:
                _add_section_subheader(pdf, "Attempts")
                for rec in attempts:
                    _add_call_block(pdf, rec, None)
                    pdf.ln(3)

            pdf.ln(4)

    # ── Transcript pages ───────────────────────────────────────────────────
    # add_page() MUST come before set_link() — calling set_link before the
    # page exists places the anchor on the wrong page.
    for link_id, rec in transcript_queue:
        pdf.add_page()
        pdf.set_link(link_id)
        _add_transcript_page(pdf, rec)

    fd, path = tempfile.mkstemp(suffix=".pdf", prefix=f"report_{dept_name[:20]}_")
    os.close(fd)
    pdf.output(path)
    return path


# ── Agent section renderers ────────────────────────────────────────────────────

def _add_agent_header(pdf: _ReportPDF, agent: str) -> None:
    """Stylized full-width banner for the agent's section."""
    pdf.set_fill_color(*_C_AGENT)
    pdf.set_draw_color(*_C_RULE)
    pdf._fmt(_C_TITLE, "B", 12)
    pdf.cell(
        _PW, 8,
        _s(agent),
        border=1, fill=True,
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        align="L",
    )
    pdf.ln(2)


def _add_agent_mini_table(pdf: _ReportPDF, agent: str, records: list[dict]) -> None:
    """Two-column mini table: Calls Attempted | Actual Conversations."""
    total  = len(records)
    convos = sum(1 for r in records if r.get("source") == "meet" or r.get("is_actual_conversation", True))

    th = FontFace(emphasis="BOLD", color=_C_TH_FG, fill_color=_C_TH_BG)

    with pdf.table(
        col_widths=_MINI_COLS,
        line_height=6,
        padding=2,
        borders_layout="MINIMAL",
    ) as table:
        hrow = table.row()
        hrow.cell("Agent / Extension",    style=th)
        hrow.cell("Calls Attempted",      style=th, align="C")
        hrow.cell("Actual Conversations", style=th, align="C")

        base = FontFace(fill_color=_C_EVEN)
        drow = table.row()
        drow.cell(_s(agent),   style=base)
        drow.cell(str(total),  style=base, align="C")
        drow.cell(str(convos), style=base, align="C")


def _add_date_header(pdf: _ReportPDF, date_str: str) -> None:
    """Large, highly visible full-width banner marking the start of a new date."""
    pdf.set_fill_color(*_C_TITLE)
    pdf.set_draw_color(*_C_RULE)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*_C_TH_FG)
    pdf.cell(
        _PW, 11,
        f"  Calls for Date: {_s(date_str)}",
        border=1, fill=True,
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        align="L",
    )
    pdf.set_text_color(*_C_BLACK)
    pdf.ln(4)


def _add_global_summary_table(pdf: _ReportPDF, records: list[dict]) -> None:
    """
    Global summary table aggregating all agents in this PDF.
    Columns: Agent / Extension | Calls Attempted | Actual Conversations
    """
    from collections import Counter
    totals: Counter = Counter()
    convos: Counter = Counter()
    for rec in records:
        agent = _s(rec.get("from", "Unknown"))
        totals[agent] += 1
        if rec.get("source") == "meet" or rec.get("is_actual_conversation", True):
            convos[agent] += 1

    th = FontFace(emphasis="BOLD", color=_C_TH_FG, fill_color=_C_TH_BG)

    with pdf.table(
        col_widths=_SUMMARY_COLS,
        line_height=6,
        padding=2,
        borders_layout="MINIMAL",
    ) as table:
        hrow = table.row()
        hrow.cell("Agent / Extension",    style=th)
        hrow.cell("Calls Attempted",      style=th, align="C")
        hrow.cell("Actual Conversations", style=th, align="C")

        for i, (agent, total) in enumerate(totals.items()):
            fill = _C_EVEN if i % 2 == 0 else (255, 255, 255)
            base = FontFace(fill_color=fill)
            drow = table.row()
            drow.cell(agent,              style=base)
            drow.cell(str(total),         style=base, align="C")
            drow.cell(str(convos[agent]), style=base, align="C")


def _add_section_subheader(pdf: _ReportPDF, title: str) -> None:
    """
    Color-coded sub-header:
      "Conversations" → bold green   (0, 128, 0)
      "Attempts"      → bold red     (200, 0, 0)
      anything else   → default title color
    Text color is reset to black after rendering.
    """
    if title == "Conversations":
        color = (0, 128, 0)
    elif title == "Attempts":
        color = (200, 0, 0)
    else:
        color = _C_TITLE

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*color)
    pdf.set_x(pdf.l_margin)
    pdf.cell(0, 6, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(*_C_BLACK)   # reset immediately after
    pdf._rule()
    pdf.ln(2)


# ── Per-call block ─────────────────────────────────────────────────────────────

def _add_call_block(pdf: _ReportPDF, rec: dict, link_id: int | None) -> None:
    """
    One full-width call block:
      header line → summary → action items → links line
    """
    sentiment  = _s(rec.get("sentiment", "Neutral"))
    source_uri = rec.get("transcript_link", "")

    pdf.ln(2)

    # ── Header line 1: Agent and call destination ──────────────────────────
    contact_name = rec.get("contact_name")
    company_name = rec.get("company_name")
    to_val       = _s(rec.get("to", "-"))

    if contact_name and company_name:
        call_to = f"{_s(contact_name)} - {_s(company_name)} ({to_val})"
    elif contact_name:
        call_to = f"{_s(contact_name)} ({to_val})"
    else:
        call_to = to_val

    pdf._fmt(_C_TITLE, "B", 10)
    pdf.set_x(pdf.l_margin)
    pdf.cell(0, 5,
             f"Agent: {_s(rec.get('from', '-'))}  |  Call To: {call_to}",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── Header line 2: Date / Time / Duration / Sentiment ─────────────────
    line2_static = (
        f"Date: {_s(rec.get('date', '-'))}  |  "
        f"Time: {_s(rec.get('time', '-'))}  |  "
        f"Duration: {_s(rec.get('duration', '-'))}  |  "
        f"Sentiment: "
    )
    pdf._fmt(_C_LABEL, "B", 10)
    pdf.set_x(pdf.l_margin)
    pdf.write(5, line2_static)

    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*_sentiment_color(sentiment))
    pdf.write(5, sentiment)
    pdf.ln(6)

    # ── Summary ────────────────────────────────────────────────────────────
    summary = _s(rec.get("summary", ""))
    if summary:
        pdf._fmt(_C_LABEL, "B", 10)
        pdf.set_x(pdf.l_margin)
        pdf.write(5, "Summary: ")
        pdf._fmt(_C_BODY, "", 10)
        pdf.write(5, summary)
        pdf.ln(5)

    # ── Action items ───────────────────────────────────────────────────────
    action_points = rec.get("action_points") or []
    if action_points:
        pdf._fmt(_C_LABEL, "B", 10)
        pdf.set_x(pdf.l_margin)
        pdf.cell(0, 5, "Action Items:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf._fmt(_C_BODY, "", 10)
        for item in action_points:
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(
                _PW - 4, 5,
                _s(f"- {item}"),
                new_x=XPos.LMARGIN, new_y=YPos.NEXT,
            )

    pdf.ln(1)

    # ── Links line ─────────────────────────────────────────────────────────
    pdf.set_x(pdf.l_margin)

    if source_uri:
        pdf.set_font("Helvetica", "U", 10)
        pdf.set_text_color(*_C_LINK)
        link_label = "Meeting Link" if rec.get("source") == "meet" else "Source Audio"
        pdf.write(5, link_label, link=source_uri)
    else:
        pdf._fmt(_C_LABEL, "", 10)
        pdf.write(5, "No Source URI")

    pdf._fmt(_C_LABEL, "", 10)
    pdf.write(5, "   |   ")

    if link_id is not None:
        pdf.set_font("Helvetica", "U", 10)
        pdf.set_text_color(*_C_LINK)
        pdf.write(5, "Full Transcript", link=link_id)
    else:
        pdf._fmt(_C_LABEL, "", 10)
        pdf.write(5, "No Transcript")

    pdf.ln(4)


# ── Transcript page ────────────────────────────────────────────────────────────

def _add_transcript_page(pdf: _ReportPDF, rec: dict) -> None:
    pdf._fmt(_C_TITLE, "B", 12)
    title = (
        f"Transcript  --  {_s(rec.get('from', 'Unknown'))}  |  "
        f"{_s(rec.get('date', ''))}"
    )
    pdf.cell(0, 7, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf._rule()
    pdf.ln(3)

    pdf._fmt(_C_LABEL, "", 9)
    meta = (
        f"To: {_s(rec.get('to', '-'))}   |   "
        f"Duration: {_s(rec.get('duration', '-'))}   |   "
        f"Sentiment: {_s(rec.get('sentiment', '-'))}"
    )
    pdf.cell(0, 5, meta, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    transcript = _s((rec.get("full_transcript") or "").strip())
    if not transcript:
        transcript = "(No transcript text available.)"

    pdf._fmt(_C_AI, "", 10)
    pdf.multi_cell(0, 5, transcript, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


# ── Utilities ──────────────────────────────────────────────────────────────────

def _sentiment_color(sentiment: str) -> tuple:
    if sentiment == "Positive":
        return _C_POS
    if sentiment in ("Negative", "Angry"):
        return _C_NEG
    return _C_NEU
