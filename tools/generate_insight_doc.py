#!/usr/bin/env python3
"""
Generate Investment Intelligence Word document.

Structure:
  - Cover: title, date range, signal count
  - Per theme (sorted by signal strength desc):
      one-liner insight | supporting articles | bull | bear | partner take
  - Deal Sourcing: top 5 companies to look at this week

Input:  .tmp/debate_memos.json, .tmp/deal_sourcing.json
Output: output/Investment_Intelligence_YYYYMMDD_YYYYMMDD.docx
"""

import os
import sys
import json
import argparse
from datetime import datetime

try:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
except ImportError:
    print("ERROR: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)

# ── Colors ─────────────────────────────────────────────────────────────────────
COLOR_BULL    = RGBColor(0x16, 0x7A, 0x47)   # green
COLOR_BEAR    = RGBColor(0xC0, 0x39, 0x2B)   # red
COLOR_PARTNER = RGBColor(0x1A, 0x53, 0xFF)   # blue
COLOR_HEADER  = RGBColor(0x1F, 0x38, 0x64)   # dark navy
COLOR_INSIGHT = RGBColor(0x2C, 0x3E, 0x50)   # dark slate
COLOR_SIGNAL  = [
    RGBColor(0xBD, 0xC3, 0xC7),  # 1 — grey
    RGBColor(0xF3, 0x9C, 0x12),  # 2 — orange
    RGBColor(0xE6, 0x7E, 0x22),  # 3 — darker orange
    RGBColor(0x27, 0xAE, 0x60),  # 4 — green
    RGBColor(0x8E, 0x44, 0xAD),  # 5 — purple (exceptional)
]

SIGNAL_LABELS = {1: "Low", 2: "Watch", 3: "Moderate", 4: "High", 5: "Exceptional"}


def _set_font(run, size=10, bold=False, color=None, font_name="Calibri"):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = font_name
    if color:
        run.font.color.rgb = color


def _para(doc, text="", size=10, bold=False, color=None, align=None, space_before=0, space_after=4):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    if align:
        p.alignment = align
    if text:
        run = p.add_run(text)
        _set_font(run, size=size, bold=bold, color=color)
    return p


def _add_hyperlink(paragraph, url: str, text: str, size=10):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    rStyle = OxmlElement("w:rStyle")
    rStyle.set(qn("w:val"), "Hyperlink")
    rPr.append(rStyle)
    new_run.append(rPr)
    new_run.text = text
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _section_divider(doc):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(4)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CCCCCC")
    pBdr.append(bottom)
    pPr.append(pBdr)


def _signal_badge(para, strength: int):
    stars = "★" * strength + "☆" * (5 - strength)
    label = SIGNAL_LABELS.get(strength, "")
    color = COLOR_SIGNAL[strength - 1] if 1 <= strength <= 5 else COLOR_SIGNAL[0]
    run = para.add_run(f"  {stars} {label} ({strength}/5)")
    _set_font(run, size=9, bold=True, color=color)


def add_theme_section(doc, memo: dict, idx: int):
    theme = memo.get("theme", "")
    insight = memo.get("insight", "")
    signal = memo.get("signal_strength", 0)
    rationale = memo.get("rationale", "")
    articles = memo.get("articles", [])
    debate = memo.get("debate", {})

    # Theme header
    p = _para(doc, f"{idx}. {theme}", size=13, bold=True, color=COLOR_HEADER,
              space_before=10, space_after=2)
    _signal_badge(p, signal)

    # One-liner insight (callout style)
    insight_para = doc.add_paragraph()
    insight_para.paragraph_format.space_before = Pt(2)
    insight_para.paragraph_format.space_after = Pt(6)
    insight_para.paragraph_format.left_indent = Inches(0.2)
    run = insight_para.add_run(f'"{insight}"')
    _set_font(run, size=10, bold=True, color=COLOR_INSIGHT)

    # Rationale
    if rationale:
        _para(doc, rationale, size=9.5, color=RGBColor(0x55, 0x55, 0x55),
              space_before=0, space_after=6)

    # Supporting articles
    if articles:
        p_label = _para(doc, "Supporting Articles", size=9, bold=True,
                        color=RGBColor(0x88, 0x88, 0x88), space_before=0, space_after=2)
        for a in articles:
            bullet = doc.add_paragraph(style="List Bullet")
            bullet.paragraph_format.space_before = Pt(0)
            bullet.paragraph_format.space_after = Pt(1)
            if a.get("url"):
                _add_hyperlink(bullet, a["url"], a.get("title", a["url"]), size=9)
                src_run = bullet.add_run(f"  ({a.get('source', '')})")
                _set_font(src_run, size=8.5, color=RGBColor(0xAA, 0xAA, 0xAA))
            else:
                run = bullet.add_run(a.get("title", ""))
                _set_font(run, size=9)

    # Bull case
    if debate.get("bull"):
        p = _para(doc, "Bull Case", size=10, bold=True, color=COLOR_BULL,
                  space_before=8, space_after=2)
        _para(doc, debate["bull"], size=9.5, space_before=0, space_after=6)

    # Bear case
    if debate.get("bear"):
        _para(doc, "Bear Case", size=10, bold=True, color=COLOR_BEAR,
              space_before=0, space_after=2)
        _para(doc, debate["bear"], size=9.5, space_before=0, space_after=6)

    # Partner take
    if debate.get("partner"):
        _para(doc, "Partner's Take", size=10, bold=True, color=COLOR_PARTNER,
              space_before=0, space_after=2)
        _para(doc, debate["partner"], size=9.5, space_before=0, space_after=6)

    _section_divider(doc)


def add_deal_sourcing_section(doc, deals: list):
    if not deals:
        return

    doc.add_page_break()
    _para(doc, "Deal Sourcing: Top Companies to Look at This Week",
          size=14, bold=True, color=COLOR_HEADER, space_before=0, space_after=8)

    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"

    headers = ["Company", "Stage", "Theme Fit", "Why Now", "Raise"]
    hdr_cells = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr_cells[i].text = h
        run = hdr_cells[i].paragraphs[0].runs[0]
        _set_font(run, size=9, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
        tc = hdr_cells[i]._tc
        tcPr = tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:fill"), "1F3864")
        shd.set(qn("w:val"), "clear")
        tcPr.append(shd)

    for deal in deals:
        row = table.add_row().cells
        # Company (hyperlinked)
        p = row[0].paragraphs[0]
        url = deal.get("url", "")
        name = deal.get("company", "")
        if url:
            _add_hyperlink(p, url, name, size=9)
        else:
            r = p.add_run(name)
            _set_font(r, size=9)

        for cell, key in zip(row[1:], ["stage", "thesis_fit", "why_now", "raise"]):
            r = cell.paragraphs[0].add_run(deal.get(key) or "—")
            _set_font(r, size=9)

    _para(doc, "", space_before=6, space_after=0)


def generate_insight_doc(memos_path: str, deals_path: str,
                          start_date: str, end_date: str, output_dir: str) -> str:
    with open(memos_path, encoding="utf-8") as f:
        memos = json.load(f)

    deals = []
    if deals_path and os.path.exists(deals_path):
        with open(deals_path, encoding="utf-8") as f:
            deals = json.load(f)

    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(1.0)
        section.right_margin = Inches(1.0)

    # Title
    _para(doc, "Investment Intelligence Report", size=20, bold=True,
          color=COLOR_HEADER, align=WD_ALIGN_PARAGRAPH.CENTER,
          space_before=0, space_after=4)
    _para(doc, f"{start_date}  →  {end_date}", size=11,
          color=RGBColor(0x77, 0x77, 0x77), align=WD_ALIGN_PARAGRAPH.CENTER,
          space_before=0, space_after=2)
    _para(doc, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  {len(memos)} themes identified",
          size=9, color=RGBColor(0xAA, 0xAA, 0xAA), align=WD_ALIGN_PARAGRAPH.CENTER,
          space_before=0, space_after=10)

    _section_divider(doc)

    # Theme sections
    for i, memo in enumerate(memos, 1):
        add_theme_section(doc, memo, i)

    # Deal sourcing
    add_deal_sourcing_section(doc, deals)

    # Save
    os.makedirs(output_dir, exist_ok=True)
    s = start_date.replace("-", "")
    e = end_date.replace("-", "")
    out_path = os.path.join(output_dir, f"Investment_Intelligence_{s}_{e}.docx")
    doc.save(out_path)
    print(f"✓ Saved → {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Generate Investment Intelligence Word doc")
    parser.add_argument("--memos",      required=True, help="debate_memos.json path")
    parser.add_argument("--deals",      default=None,  help="deal_sourcing.json path (optional)")
    parser.add_argument("--start_date", required=True)
    parser.add_argument("--end_date",   required=True)
    parser.add_argument("--output_dir", default="output")
    args = parser.parse_args()

    generate_insight_doc(args.memos, args.deals, args.start_date, args.end_date, args.output_dir)


if __name__ == "__main__":
    main()
