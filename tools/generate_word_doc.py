#!/usr/bin/env python3
"""
Generate Word document with AI News table:
- Title (hyperlinked) + Date + Source | Summary (with optional Chinese translation)

Uses python-docx for formatting
"""

import os
import sys
import json
import re
import argparse
import time
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import format_date_for_display
from tools.detect_funding import extract_funding_with_claude

try:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
except ImportError:
    print("ERROR: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)

FONT_ENGLISH = "Microsoft YaHei"
FONT_CHINESE = "微软雅黑"


def set_run_font(run, font_size: int = 10):
    """Set consistent ASCII and East Asian fonts on a run."""
    run.font.size = Pt(font_size)
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.insert(0, rFonts)
    # Set explicit fonts
    rFonts.set(qn('w:ascii'), FONT_ENGLISH)
    rFonts.set(qn('w:hAnsi'), FONT_ENGLISH)
    rFonts.set(qn('w:eastAsia'), FONT_CHINESE)
    rFonts.set(qn('w:cs'), FONT_ENGLISH)
    # Remove theme font attributes — in OOXML, theme attributes take precedence
    # over explicit font names and will cause inconsistency if left in place.
    for attr in (qn('w:asciiTheme'), qn('w:hAnsiTheme'), qn('w:eastAsiaTheme'), qn('w:cstheme')):
        if attr in rFonts.attrib:
            del rFonts.attrib[attr]


def _set_para_font(para):
    """Set consistent font names on every run in a paragraph without touching font size.
    Used for headings and plain paragraphs so the style-defined size is preserved."""
    for run in para.runs:
        rPr = run._r.get_or_add_rPr()
        rFonts = rPr.find(qn('w:rFonts'))
        if rFonts is None:
            rFonts = OxmlElement('w:rFonts')
            rPr.insert(0, rFonts)
        rFonts.set(qn('w:ascii'), FONT_ENGLISH)
        rFonts.set(qn('w:hAnsi'), FONT_ENGLISH)
        rFonts.set(qn('w:eastAsia'), FONT_CHINESE)
        rFonts.set(qn('w:cs'), FONT_ENGLISH)
        for attr in (qn('w:asciiTheme'), qn('w:hAnsiTheme'), qn('w:eastAsiaTheme'), qn('w:cstheme')):
            if attr in rFonts.attrib:
                del rFonts.attrib[attr]

def _set_table_cell_margins(table, top: int = 80, bottom: int = 80,
                             left: int = 100, right: int = 100):
    """Set uniform cell padding (twips) on all cells in a table."""
    tbl = table._tbl
    tblPr = tbl.find(qn('w:tblPr'))
    if tblPr is None:
        tblPr = OxmlElement('w:tblPr')
        tbl.insert(0, tblPr)
    existing = tblPr.find(qn('w:tblCellMar'))
    if existing is not None:
        tblPr.remove(existing)
    tblCellMar = OxmlElement('w:tblCellMar')
    for side, val in [('top', top), ('bottom', bottom), ('left', left), ('right', right)]:
        node = OxmlElement(f'w:{side}')
        node.set(qn('w:w'), str(val))
        node.set(qn('w:type'), 'dxa')
        tblCellMar.append(node)
    tblPr.append(tblCellMar)


def _add_horizontal_rule(doc: Document, color: str = '4472C4'):
    """Add a thin paragraph-border line as a visual section divider."""
    para = doc.add_paragraph()
    para.paragraph_format.space_before = Pt(2)
    para.paragraph_format.space_after = Pt(8)
    pPr = para._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), color)
    pBdr.append(bottom)
    pPr.append(pBdr)


try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)


def add_hyperlink(paragraph, url: str, text: str, font_size: int = 10):
    """
    Add a hyperlink to a paragraph

    python-docx doesn't have native hyperlink support, so we use XML

    Args:
        paragraph: docx paragraph object
        url: URL to link to
        text: Link text
        font_size: Font size in points
    """
    # Create hyperlink element
    part = paragraph.part
    r_id = part.relate_to(url, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink', is_external=True)

    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)

    # Create run element
    run = OxmlElement('w:r')

    # Run properties (style)
    rPr = OxmlElement('w:rPr')

    # Font names (ASCII + East Asian)
    rFonts = OxmlElement('w:rFonts')
    rFonts.set(qn('w:ascii'), FONT_ENGLISH)
    rFonts.set(qn('w:hAnsi'), FONT_ENGLISH)
    rFonts.set(qn('w:eastAsia'), FONT_CHINESE)
    rFonts.set(qn('w:cs'), FONT_ENGLISH)
    rPr.append(rFonts)

    # Font size (half-points)
    sz = OxmlElement('w:sz')
    sz.set(qn('w:val'), str(font_size * 2))
    rPr.append(sz)
    szCs = OxmlElement('w:szCs')
    szCs.set(qn('w:val'), str(font_size * 2))
    rPr.append(szCs)

    # Blue color
    color = OxmlElement('w:color')
    color.set(qn('w:val'), '0563C1')
    rPr.append(color)

    # Underline
    u = OxmlElement('w:u')
    u.set(qn('w:val'), 'single')
    rPr.append(u)

    run.append(rPr)

    # Add text
    text_elem = OxmlElement('w:t')
    text_elem.text = text
    run.append(text_elem)

    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def add_formatted_text(paragraph, text: str, font_size: int = 10):
    """
    Add text to paragraph with markdown bold (**text**) converted to Word bold

    Args:
        paragraph: docx paragraph object
        text: Text that may contain **bold** markdown
        font_size: Font size in points
    """
    # Pattern to match **bold** text
    pattern = r'\*\*(.+?)\*\*'

    last_end = 0
    for match in re.finditer(pattern, text):
        # Add text before the bold part
        if match.start() > last_end:
            run = paragraph.add_run(text[last_end:match.start()])
            set_run_font(run, font_size=font_size)

        # Add bold text
        run = paragraph.add_run(match.group(1))
        run.bold = True
        set_run_font(run, font_size=font_size)

        last_end = match.end()

    # Add remaining text after last match
    if last_end < len(text):
        run = paragraph.add_run(text[last_end:])
        set_run_font(run, font_size=font_size)


def translate_to_chinese_claude(api_key: str, text: str) -> str:
    """
    Translate text to Chinese using Claude API

    Args:
        api_key: Anthropic API key
        text: Text to translate

    Returns:
        Chinese translation
    """
    try:
        url = "https://api.anthropic.com/v1/messages"

        headers = {
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": f"将以下文本翻译成简体中文。只输出翻译结果，不要其他说明。\n\n{text}"
                }
            ]
        }

        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        result = response.json()

        if 'content' in result and len(result['content']) > 0:
            return result['content'][0]['text'].strip()
        else:
            print(f"  WARNING: Unexpected Claude response format")
            return ""

    except Exception as e:
        print(f"  WARNING: Translation failed: {e}")
        return ""


def _search_funding_single_day(api_key: str, date: str, topic: str) -> list:
    """
    Search for funding events on a single date using OpenAI web search.
    Returns list of funding event dicts.
    """
    if topic == 'deeptech':
        sector_desc = "深科技公司（包括机器人、先进材料、量子计算、生物/医疗科技、航天、半导体、清洁能源等硬科技领域）"
    else:
        sector_desc = (
            "以AI/人工智能为核心技术的公司。"
            "纳入范围：大语言模型、生成式AI、AI agent、计算机视觉、语音AI、AI基础设施、AI驱动的SaaS产品。"
            "排除范围：传统数据存储、传统网络安全（非AI核心）、普通云计算、区块链/加密货币、"
            "传统金融科技、以及仅将AI作为边缘功能的传统软件公司"
        )

    prompt = f"""搜索网络，找出{date}宣布的{sector_desc}融资轮次、投资和收购事件。

对于每个融资事件，返回一个JSON对象。返回一个包含以下字段的JSON数组：
- "date": 宣布日期，格式为YYYY-MM-DD
- "company": 获得融资的公司名称
- "summary": 用中文描述该公司，包含：(1) 一句话说明公司的核心业务，(2) 如网上有创始人相关背景信息，请附上（例如：曾就职的知名公司、负责的项目、相关行业经验等）。参考格式："AI-native 网络安全公司，用 AI agent 实时检测攻击并自动响应。创始人 XX 曾负责 Amazon Web Services GuardDuty，联合创始人 YY 曾在 Abnormal AI 负责机器学习"
- "stage": 融资轮次（天使轮、Pre-A轮、A轮、B轮、C轮等，如为收购则填"收购"，未知填"不详"）
- "raise": 融资金额（例如："5000万美元"、"12亿美元"，未知填"不详"）
- "valuation": 融资后估值（例如："5亿美元"、"12亿美元"，未知填"不详"）
- "investors": 主要投资方（例如："领投：红杉资本，跟投：Andreessen Horowitz"，未知填"不详"）
- "url": 最相关的新闻来源链接（如有则填完整URL，否则填""）

只包含实际融资事件（已筹集资金、收购、IPO）。如未找到任何事件，返回空数组[]。
仅返回有效的JSON数组，不要包含其他文字。"""

    try:
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        payload = {
            "model": "gpt-4o-search-preview",
            "web_search_options": {},
            "messages": [{"role": "user", "content": prompt}],
        }

        for attempt in range(3):
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload, headers=headers, timeout=90
            )
            if response.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            break

        result = response.json()
        if 'choices' not in result:
            print(f"  OpenAI response: {result}")
            return []

        content = result['choices'][0]['message']['content'].strip()

        json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', content, re.DOTALL)
        if json_match:
            content = json_match.group(1)
        else:
            array_match = re.search(r'(\[.*\])', content, re.DOTALL)
            if array_match:
                content = array_match.group(1)
            else:
                return []

        events = json.loads(content)
        return events if isinstance(events, list) else []

    except Exception as e:
        print(f"  WARNING: Funding search failed for {date}: {e}")
        return []


def _filter_ai_funding_events(claude_key: str, events: list) -> list:
    """
    Use Claude to validate that each funding event is genuinely AI-focused.
    Batches all companies in one API call to minimise cost.
    Returns the filtered list.
    """
    if not events or not claude_key:
        return events

    companies = [
        {"index": i, "company": e.get('company', ''), "summary": e.get('summary', '')}
        for i, e in enumerate(events)
    ]

    prompt = (
        "以下是一批融资新闻公司，请判断每家公司是否以AI/人工智能为核心业务。\n\n"
        "判断标准：\n"
        "- 是：公司的主要产品/服务以AI/ML为技术基础（大语言模型、生成式AI、AI agent、"
        "计算机视觉、AI-native SaaS等）\n"
        "- 否：AI只是次要功能，或公司属于传统数据存储、传统网络安全、"
        "普通云基础设施、区块链/加密、传统金融科技\n\n"
        f"公司列表：\n{json.dumps(companies, ensure_ascii=False)}\n\n"
        "返回一个JSON数组，只包含确实是AI公司的index值。例如：[0, 2, 4]\n"
        "只返回JSON数组，不含其他文字。"
    )

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={
                'Content-Type': 'application/json',
                'x-api-key': claude_key,
                'anthropic-version': '2023-06-01',
            },
            timeout=30,
        )
        response.raise_for_status()
        text = response.json()['content'][0]['text'].strip()
        match = re.search(r'\[[\d,\s]*\]', text)
        if match:
            keep_indices = set(json.loads(match.group()))
            filtered = [e for i, e in enumerate(events) if i in keep_indices]
            removed = len(events) - len(filtered)
            if removed:
                print(f"  Validation removed {removed} non-AI company/companies")
            return filtered
    except Exception as e:
        print(f"  WARNING: AI validation failed ({e}), keeping all events")

    return events


def _company_key(name: str) -> str:
    """Normalize a company name to a dedup key.

    Strips Chinese characters first so that bilingual names like
    "谷歌母公司 Alphabet" and "Alphabet" collapse to the same key.
    Falls back to the full lowercased name for Chinese-only companies.
    """
    english = re.sub(r'[一-鿿　-〿＀-￯\s]+', ' ', name).strip().lower()
    return english if english else name.lower().strip()


def _merge_funding_events(base: list, extra: list) -> list:
    """Merge two funding event lists, deduplicating by company name.

    'base' events take priority — they come from Claude's extraction of our
    collected articles (higher fidelity). 'extra' events from the OpenAI web
    search are only added if the company wasn't already found in base.
    When two entries for the same company exist, the one with more non-unknown
    fields is kept (fewest '不详'/'N/A' values wins).
    """
    seen: dict = {}
    for e in base + extra:
        raw_name = e.get('company', '')
        if not raw_name:
            continue
        key = _company_key(raw_name)
        if key not in seen:
            seen[key] = e
        else:
            # Keep whichever entry has more filled-in fields
            existing = seen[key]
            existing_score = sum(1 for v in existing.values() if str(v) not in ('不详', '', 'N/A', None))
            new_score = sum(1 for v in e.values() if str(v) not in ('不详', '', 'N/A', None))
            if new_score > existing_score:
                seen[key] = e
    return list(seen.values())


def extract_funding_with_openai(api_key: str, start_date: str, end_date: str,
                               topic: str = 'ai', claude_key: str = None) -> list:
    """
    Use OpenAI with web search to find funding events in a date range.
    Searches day-by-day to avoid the model skipping dates in long ranges.

    Args:
        api_key: OpenAI API key
        start_date: YYYY-MM-DD start date
        end_date: YYYY-MM-DD end date
        topic: 'ai' (default) or 'deeptech'

    Returns:
        List of funding event dicts with keys: date, company, summary, stage, raise, valuation, investors
    """
    from datetime import timedelta

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    total_days = (end_dt - start_dt).days + 1
    print(f"  Searching {total_days} day(s): {start_date} to {end_date}")

    all_events = []
    current = start_dt
    day_num = 0
    while current <= end_dt:
        day_num += 1
        date_str = current.strftime("%Y-%m-%d")
        print(f"  [{day_num}/{total_days}] Searching {date_str}...")
        events = _search_funding_single_day(api_key, date_str, topic)
        print(f"    Found {len(events)} events")
        all_events.extend(events)
        current += timedelta(days=1)
        time.sleep(1.0)  # rate limiting between days

    unique = _merge_funding_events(all_events, [])
    print(f"  Total unique funding events: {len(unique)}")

    # Drop events outside the requested date range — the OpenAI web search
    # often returns historical results regardless of the date specified.
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        in_range = []
        out_of_range = 0
        for e in unique:
            event_date_str = e.get('date', '')[:10]
            try:
                event_dt = datetime.strptime(event_date_str, "%Y-%m-%d")
                if start_dt <= event_dt <= end_dt:
                    in_range.append(e)
                else:
                    out_of_range += 1
            except ValueError:
                in_range.append(e)  # keep events with unparseable dates
        if out_of_range:
            print(f"  Removed {out_of_range} event(s) outside {start_date}–{end_date}")
        unique = in_range
    except Exception as e:
        print(f"  WARNING: Date filtering failed ({e}), keeping all events")

    # For AI topic, validate each event is genuinely AI-focused
    if topic == 'ai' and claude_key:
        print(f"  Validating AI relevance...")
        unique = _filter_ai_funding_events(claude_key, unique)

    return unique


def _funding_priority(event: dict) -> tuple:
    """Return (label, fill_hex) for VC prioritization based on funding stage.

    Early-stage rounds are highest priority because there is still room to invest
    at a reasonable valuation. Later-stage / IPO events are informational only.
    """
    stage = (event.get('stage') or '').lower().strip()
    if any(k in stage for k in ('seed', 'pre', 'angel', 'a轮', 'series a', '天使', '种子')):
        return ('重点关注', 'F4CCCC')   # light red — early stage, act fast
    if any(k in stage for k in ('series b', 'b轮', 'b+')):
        return ('值得关注', 'FFF2CC')   # light yellow — still interesting
    if not stage or stage in ('不详', 'n/a', 'na', 'unknown'):
        return ('值得关注', 'FFF2CC')   # light yellow — unknown, worth checking
    return ('一般了解', 'F3F3F3')       # light gray — Series C+, IPO, acquisition


def create_funding_table(doc: Document, funding_events: list, heading: str = 'AI 融资动态'):
    """
    Add Fundraising News section with a 7-column table (all Chinese).

    Args:
        doc: Document object
        funding_events: List of funding event dicts
        heading: Section heading text
    """
    doc.add_paragraph('')
    h = doc.add_heading(heading, level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_para_font(h)

    if not funding_events:
        _set_para_font(doc.add_paragraph('该时间段内未发现融资事件。'))
        return

    _set_para_font(doc.add_paragraph(f'共 {len(funding_events)} 条融资记录\n'))

    # Create table with 8 columns (added 优先级 after company)
    table = doc.add_table(rows=1, cols=8)
    table.style = 'Light Grid Accent 1'

    col_widths = [Inches(0.65), Inches(0.85), Inches(0.7), Inches(1.4), Inches(0.55), Inches(0.6), Inches(0.6), Inches(1.2)]
    for i, width in enumerate(col_widths):
        table.columns[i].width = width
    _set_table_cell_margins(table)

    # Header row (Chinese)
    headers = ['日期', '公司', '优先级', '概述', '轮次', '融资额', '估值', '投资方']
    header_cells = table.rows[0].cells
    for i, h_text in enumerate(headers):
        header_cells[i].text = h_text
        for run in header_cells[i].paragraphs[0].runs:
            run.bold = True
            set_run_font(run, font_size=10)

    # Sort by date oldest first
    funding_events.sort(key=lambda x: x.get('date', ''))

    # Data rows
    for event in funding_events:
        row_cells = table.add_row().cells

        # Col 0: date
        date_run = row_cells[0].paragraphs[0].add_run(event.get('date', '')[:10])
        set_run_font(date_run, font_size=9)

        # Col 1: company name, hyperlinked if URL available
        company = event.get('company', '不详')
        url = event.get('url', event.get('_url', ''))
        company_para = row_cells[1].paragraphs[0]
        if url:
            add_hyperlink(company_para, url, company, font_size=9)
        else:
            run = company_para.add_run(company)
            set_run_font(run, font_size=9)

        # Col 2: priority badge with background shading
        label, fill_hex = _funding_priority(event)
        priority_cell = row_cells[2]
        tcPr = priority_cell._tc.get_or_add_tcPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:color'), 'auto')
        shd.set(qn('w:fill'), fill_hex)
        tcPr.append(shd)
        priority_run = priority_cell.paragraphs[0].add_run(label)
        priority_run.bold = True
        set_run_font(priority_run, font_size=9)

        # Cols 3-7: remaining fields
        remaining = [
            event.get('summary', '不详'),
            event.get('stage', '不详'),
            event.get('raise', '不详'),
            event.get('valuation', '不详'),
            event.get('investors', '不详'),
        ]
        for i, val in enumerate(remaining, start=3):
            para = row_cells[i].paragraphs[0]
            run = para.add_run(str(val))
            set_run_font(run, font_size=9)


def convert_bullets_to_paragraph(text: str) -> str:
    """
    Convert bullet point text to paragraph format.
    Removes bullet markers and joins into flowing text.

    Args:
        text: Text with potential bullet points

    Returns:
        Text as paragraph without bullets
    """
    # Remove common bullet markers and clean up
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        # Remove bullet markers
        line = re.sub(r'^[\-\*•]\s*', '', line)
        line = re.sub(r'^\d+[\.\)]\s*', '', line)
        if line:
            cleaned_lines.append(line)

    # Join with spaces to form paragraph
    return ' '.join(cleaned_lines)


# ── AI news category grouping ──────────────────────────────────────────────────

AI_NEWS_CATEGORY_ORDER = ["模型与研究", "产品与应用", "大科技公司", "政策与安全", "行业动态", "其他"]

AI_NEWS_CATEGORY_COLORS = {
    "模型与研究": "DDEEFF",
    "产品与应用": "FFF8E1",
    "大科技公司": "E6F4EA",
    "政策与安全": "FCE8E8",
    "行业动态": "F3E5F5",
    "其他":      "F5F5F5",
}

# ── Deeptech category grouping ─────────────────────────────────────────────────

DEEPTECH_CATEGORY_ORDER = ["半导体", "机器人", "新能源", "其他"]

DEEPTECH_CATEGORY_COLORS = {
    "半导体": "E8F0FE",
    "机器人": "E6F4EA",
    "新能源": "FFF8E1",
    "其他":   "F3E5F5",
}

SEMICONDUCTOR_KEYWORDS = [
    "semiconductor", "chip", "芯片", "半导体", "wafer", "fab", "foundry",
    "transistor", "lithography", "eda", "photonic", "asic", "fpga",
    "nvidia", "intel", "amd", "tsmc", "arm ", "risc", "memory", "dram",
    "nand", "soc", "gpu", "cpu", "mpu", "integrated circuit",
]
ROBOTICS_KEYWORDS = [
    "robot", "机器人", "humanoid", "autonomous", "drone", "无人机",
    "unmanned", "exoskeleton", "cobots", "manipulation", "locomotion",
    "actuator", "servo", "mechatronics", "automation",
]
ENERGY_KEYWORDS = [
    "energy", "新能源", "electric vehicle", "ev ", " ev\n", "solar",
    "battery", "电池", "储能", "充电", "wind", "nuclear", "hydrogen",
    "fuel cell", "grid", "power", "renewable", "carbon", "climate",
    "clean tech", "cleantech", "charging", "inverter", "photovoltaic",
]


def classify_deeptech_article(article: dict) -> str:
    """Classify a deeptech article into 半导体 | 机器人 | 新能源 | 其他."""
    text = " " + " ".join([
        article.get("title", ""),
        article.get("summary", ""),
        article.get("description", ""),
    ]).lower() + " "

    for kw in SEMICONDUCTOR_KEYWORDS:
        if kw in text:
            return "半导体"
    for kw in ROBOTICS_KEYWORDS:
        if kw in text:
            return "机器人"
    for kw in ENERGY_KEYWORDS:
        if kw in text:
            return "新能源"
    return "其他"


def _add_deeptech_header_row(table, label: str, fill_hex: str, ncols: int = 2):
    """Add a full-width merged header row for category sections."""
    row = table.add_row()
    row.cells[0].merge(row.cells[ncols - 1])
    cell = row.cells[0]

    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()

    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), fill_hex)
    tcPr.append(shd)

    # Left accent border
    tcBdr = OxmlElement('w:tcBdr')
    left_bdr = OxmlElement('w:left')
    left_bdr.set(qn('w:val'), 'single')
    left_bdr.set(qn('w:sz'), '18')
    left_bdr.set(qn('w:space'), '0')
    left_bdr.set(qn('w:color'), '1F497D')
    tcBdr.append(left_bdr)
    tcPr.append(tcBdr)

    para = cell.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    para.paragraph_format.space_before = Pt(3)
    para.paragraph_format.space_after = Pt(3)
    run = para.add_run(label)
    run.bold = True
    set_run_font(run, font_size=11)
    run.font.color.rgb = RGBColor(31, 73, 125)


def create_grouped_deeptech_table(
    doc: Document,
    articles: list,
    chinese_only: bool = False,
    translate: bool = False,
    claude_key: str = None,
    heading: str = '深科技新闻摘要',
):
    """Build a grouped deeptech news table: 半导体 → 机器人 → 新能源 → 其他."""
    groups = {cat: [] for cat in DEEPTECH_CATEGORY_ORDER}
    for article in articles:
        groups[classify_deeptech_article(article)].append(article)

    for cat in DEEPTECH_CATEGORY_ORDER:
        groups[cat].sort(key=lambda x: x.get("published_at", ""))

    total = sum(len(v) for v in groups.values())

    h = doc.add_heading(heading, level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_para_font(h)
    doc.add_paragraph(f'共 {total} 篇\n')

    table = doc.add_table(rows=1, cols=2)
    table.style = 'Light Grid Accent 1'
    table.columns[0].width = Inches(0.85)
    table.columns[1].width = Inches(5.65)
    _set_table_cell_margins(table)

    hdr = table.rows[0].cells
    hdr[0].text = '日期'
    hdr[1].text = '摘要'
    for cell in hdr:
        for para in cell.paragraphs:
            for run in para.runs:
                run.bold = True
                set_run_font(run, font_size=10)

    for cat in DEEPTECH_CATEGORY_ORDER:
        cat_articles = groups[cat]
        if not cat_articles:
            continue

        _add_deeptech_header_row(table, cat, DEEPTECH_CATEGORY_COLORS[cat])

        for article in cat_articles:
            row_cells = table.add_row().cells

            date_para = row_cells[0].paragraphs[0]
            date_para.paragraph_format.space_before = Pt(4)
            set_run_font(date_para.add_run(format_date_for_display(article.get('published_at', ''))), font_size=9)

            _fill_summary_cell(row_cells[1], article, translate, claude_key, chinese_only)

    return table


# Short Chinese badge labels appended inline after each article title in the Word doc.
# 'other' is intentionally omitted — no badge is shown for generic news, keeping
# the visual noise low and making the meaningful signals stand out.
VC_SIGNAL_LABELS = {
    'funding':     '[融资]',
    'product':     '[产品]',
    'partnership': '[合作]',
    'hire':        '[人事]',
    'regulatory':  '[监管]',
    'research':    '[研究]',
}

# Each signal type gets a distinct color so a reader can scan by color at a glance.
VC_SIGNAL_COLORS = {
    'funding':     RGBColor(0, 112, 192),   # blue
    'product':     RGBColor(0, 128, 0),     # green
    'partnership': RGBColor(112, 48, 160),  # purple
    'hire':        RGBColor(197, 90, 17),   # orange
    'regulatory':  RGBColor(192, 0, 0),     # red
    'research':    RGBColor(31, 73, 125),   # dark blue
}


def _load_watchlist(watchlist_file: str) -> list:
    """Load company names from watchlist.txt, one per line. Lines starting with # are comments."""
    if not watchlist_file or not os.path.exists(watchlist_file):
        # Silently skip if no file — watchlist is opt-in, not required
        return []
    with open(watchlist_file, 'r', encoding='utf-8') as f:
        companies = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    if companies:
        print(f"Watchlist loaded: {len(companies)} companies ({', '.join(companies[:5])}{'...' if len(companies) > 5 else ''})")
    return companies


def _check_watchlist(article: dict, watchlist: list) -> list:
    """Return watchlist company names found in article title or summary.

    Checks title + summary + description so it catches both original articles
    and TLDR-style blurbs where the company name only appears in the description.
    """
    text = ' '.join([
        article.get('title', ''),
        article.get('summary', ''),
        article.get('description', ''),
    ]).lower()
    return [co for co in watchlist if co.lower() in text]


def create_watchlist_section(doc: Document, articles: list,
                              translate: bool, claude_key: str, chinese_only: bool):
    """Add a Watchlist Highlights section at the top of the document.

    Appears before the main AI News table so investment team members can
    immediately see news about companies they're actively tracking.
    """
    h = doc.add_heading('关注公司动态', level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_para_font(h)

    intro = doc.add_paragraph(f'共 {len(articles)} 篇提及关注公司的文章\n')
    _set_para_font(intro)

    table = doc.add_table(rows=1, cols=3)
    table.style = 'Light Grid Accent 1'
    table.columns[0].width = Inches(0.75)
    table.columns[1].width = Inches(0.45)
    table.columns[2].width = Inches(5.3)
    _set_table_cell_margins(table)

    hdr = table.rows[0].cells
    for i, txt in enumerate(['日期', '优先级', '摘要']):
        hdr[i].text = txt
        for run in hdr[i].paragraphs[0].runs:
            run.bold = True
            set_run_font(run, font_size=10)
        if i == 1:
            hdr[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    articles_sorted = sorted(articles, key=lambda x: (-x.get('relevance', 3), x.get('published_at', '')))
    for idx, article in enumerate(articles_sorted):
        _add_article_row(table, article, translate, claude_key, chinese_only,
                         idx, len(articles_sorted), show_priority=True)


def _fill_summary_cell(cell, article: dict, translate: bool, claude_key: str,
                        chinese_only: bool):
    """Fill the summary cell: hyperlinked title + body paragraph(s) with proper spacing."""
    title = article.get('title', '无标题')
    url = article.get('url', '')
    vc_signal = article.get('vc_signal', '')

    title_para = cell.paragraphs[0]
    title_para.paragraph_format.space_after = Pt(3)
    if url:
        add_hyperlink(title_para, url, title, font_size=10)
    else:
        run = title_para.add_run(title)
        run.bold = True
        set_run_font(run, font_size=10)

    # Append a colored VC signal badge inline with the title (e.g. "  [融资]" in blue).
    # 'other' is skipped — only named signal types get a badge to avoid visual clutter.
    if vc_signal and vc_signal != 'other' and vc_signal in VC_SIGNAL_LABELS:
        badge_run = title_para.add_run('  ' + VC_SIGNAL_LABELS[vc_signal])
        badge_run.bold = True
        set_run_font(badge_run, font_size=9)
        badge_run.font.color.rgb = VC_SIGNAL_COLORS[vc_signal]

    summary = article.get('summary', article.get('description', '暂无摘要'))
    summary_text = convert_bullets_to_paragraph(summary)

    if chinese_only:
        body = cell.add_paragraph()
        body.paragraph_format.space_before = Pt(0)
        body.paragraph_format.space_after = Pt(4)
        add_formatted_text(body, summary_text, font_size=10)
    elif translate and claude_key:
        chinese = translate_to_chinese_claude(claude_key, summary_text)
        if chinese:
            cn_para = cell.add_paragraph()
            cn_para.paragraph_format.space_before = Pt(0)
            cn_para.paragraph_format.space_after = Pt(3)
            add_formatted_text(cn_para, chinese, font_size=10)
            time.sleep(0.5)
        en_para = cell.add_paragraph()
        en_para.paragraph_format.space_before = Pt(0)
        en_para.paragraph_format.space_after = Pt(4)
        add_formatted_text(en_para, summary_text, font_size=10)
    else:
        body = cell.add_paragraph()
        body.paragraph_format.space_before = Pt(0)
        body.paragraph_format.space_after = Pt(4)
        add_formatted_text(body, summary_text, font_size=10)


_PRIORITY_COLORS = {
    5: RGBColor(31, 73, 125),   # dark blue
    4: RGBColor(68, 114, 196),  # medium blue
    3: RGBColor(89, 89, 89),    # dark gray
    2: RGBColor(128, 128, 128), # gray
    1: RGBColor(166, 166, 166), # light gray
}


def _add_article_row(table, article: dict, translate: bool, claude_key: str,
                     chinese_only: bool, idx: int, total: int,
                     show_priority: bool = False):
    """Write one article as a table row. Shared by flat and grouped layouts."""
    if translate:
        print(f"  [{idx+1}/{total}] Translating...")

    row_cells = table.add_row().cells

    date_para = row_cells[0].paragraphs[0]
    date_para.paragraph_format.space_before = Pt(4)
    date_run = date_para.add_run(format_date_for_display(article.get('published_at', '')))
    set_run_font(date_run, font_size=9)

    if show_priority:
        relevance = article.get('relevance', 3)
        p_para = row_cells[1].paragraphs[0]
        p_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_para.paragraph_format.space_before = Pt(4)
        p_run = p_para.add_run(str(relevance))
        p_run.bold = True
        set_run_font(p_run, font_size=13)
        p_run.font.color.rgb = _PRIORITY_COLORS.get(relevance, RGBColor(89, 89, 89))
        _fill_summary_cell(row_cells[2], article, translate, claude_key, chinese_only)
    else:
        _fill_summary_cell(row_cells[1], article, translate, claude_key, chinese_only)


def create_news_table(doc: Document, articles: list, max_articles: int = None,
                      translate: bool = False, claude_key: str = None,
                      chinese_only: bool = False):
    """Create AI News table. Groups by category + shows priority column when articles are categorized."""
    if max_articles:
        articles = articles[:max_articles]

    heading = doc.add_heading('AI 新闻摘要', level=1)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_para_font(heading)
    doc.add_paragraph(f'共 {len(articles)} 篇\n')

    is_categorized = any('category' in a for a in articles)

    if is_categorized:
        # 3-column table: Date | 优先级 | Summary
        table = doc.add_table(rows=1, cols=3)
        table.style = 'Light Grid Accent 1'
        table.columns[0].width = Inches(0.75)
        table.columns[1].width = Inches(0.45)
        table.columns[2].width = Inches(5.3)
        _set_table_cell_margins(table)

        hdr = table.rows[0].cells
        for i, txt in enumerate(['日期', '优先级', '摘要']):
            hdr[i].text = txt
            for run in hdr[i].paragraphs[0].runs:
                run.bold = True
                set_run_font(run, font_size=10)
            if i == 1:
                hdr[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
    else:
        # 2-column flat table: Date | Summary
        table = doc.add_table(rows=1, cols=2)
        table.style = 'Light Grid Accent 1'
        table.columns[0].width = Inches(0.85)
        table.columns[1].width = Inches(5.65)
        _set_table_cell_margins(table)

        hdr = table.rows[0].cells
        hdr[0].text = '日期'
        hdr[1].text = '摘要'
        for cell in hdr:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.bold = True
                    set_run_font(run, font_size=10)

    if is_categorized:
        groups: dict = {cat: [] for cat in AI_NEWS_CATEGORY_ORDER}
        for article in articles:
            cat = article.get('category', '其他')
            if cat not in groups:
                cat = '其他'
            groups[cat].append(article)

        idx = 0
        for cat in AI_NEWS_CATEGORY_ORDER:
            cat_articles = groups[cat]
            if not cat_articles:
                continue
            cat_articles.sort(key=lambda x: (-x.get('relevance', 3), x.get('published_at', '')))
            _add_deeptech_header_row(table, cat, AI_NEWS_CATEGORY_COLORS[cat], ncols=3)
            for article in cat_articles:
                _add_article_row(table, article, translate, claude_key, chinese_only,
                                 idx, len(articles), show_priority=True)
                idx += 1
    else:
        articles.sort(key=lambda x: x.get('published_at', ''))
        for i, article in enumerate(articles):
            _add_article_row(table, article, translate, claude_key, chinese_only,
                             i, len(articles), show_priority=False)

    return table


def generate_word_doc(start_date: str, end_date: str,
                      articles_file: str = '.tmp/summarized_articles.json',
                      output_dir: str = 'output',
                      max_articles: int = None,
                      translate: bool = False,
                      chinese_only: bool = False,
                      output_prefix: str = 'AI_News',
                      funding_topic: str = 'ai',
                      doc_title: str = None,
                      min_signal: int = 1,
                      watchlist_file: str = 'watchlist.txt'):
    """
    Main document generation function

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        articles_file: Path to summarized articles JSON
        output_dir: Output directory
        max_articles: Maximum articles to include (None = all)
        translate: Add Chinese translation using Claude
    """
    load_dotenv()

    print("Loading data...")

    # Load summarized articles
    if not os.path.exists(articles_file):
        print(f"ERROR: {articles_file} not found")
        print("Make sure to run summarize_articles.py first")
        sys.exit(1)

    with open(articles_file, 'r', encoding='utf-8') as f:
        articles = json.load(f)

    print(f"Loaded {len(articles)} articles")

    # Drop articles below the requested relevance threshold before building the doc.
    # Scores were assigned by Claude during summarization (1=low value, 5=must-read VC signal).
    # Default is 1 (keep everything); pass --min-signal 3 to cut noise for a tighter brief.
    # Articles that have no 'relevance' field (e.g. from an older run) default to 3.
    if min_signal > 1:
        before = len(articles)
        articles = [a for a in articles if a.get('relevance', 3) >= min_signal]
        print(f"Signal filter (>= {min_signal}): keeping {len(articles)}/{before} articles")

    # Load the watchlist of investment-target company names (edit watchlist.txt to configure).
    # Returns an empty list if the file doesn't exist, so the watchlist section is skipped.
    watchlist = _load_watchlist(watchlist_file)

    # Load Claude key — needed for translation and funding validation
    claude_key = os.getenv('ANTHROPIC_API_KEY')
    if translate:
        if not claude_key:
            print("ERROR: ANTHROPIC_API_KEY not found in .env file")
            sys.exit(1)
        print("Translation enabled (Claude)")
    elif claude_key:
        print("Claude key loaded (used for funding validation)")

    # Check for OpenAI key for funding extraction
    openai_key = os.getenv('OPENAI_API_KEY')

    # Create document
    print("Creating Word document...")
    doc = Document()

    # Set 1" margins — gives 6.5" content width on letter paper
    section = doc.sections[0]
    section.left_margin = Inches(1.0)
    section.right_margin = Inches(1.0)
    section.top_margin = Inches(1.0)
    section.bottom_margin = Inches(1.0)

    # Title
    display_title = doc_title if doc_title else 'AI 资讯报告'
    title = doc.add_heading(display_title, level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_para_font(title)

    # Subtitle with date range
    subtitle = doc.add_paragraph(f'{start_date} 至 {end_date}')
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in subtitle.runs:
        set_run_font(run, font_size=14)
        run.font.color.rgb = RGBColor(128, 128, 128)

    # Compact metadata line
    meta_para = doc.add_paragraph()
    meta_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta_run = meta_para.add_run(
        f'生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}  ·  '
        f'文章总数：{len(articles)}'
    )
    set_run_font(meta_run, font_size=9)
    meta_run.font.color.rgb = RGBColor(140, 140, 140)

    _add_horizontal_rule(doc)

    # Watchlist highlights: scan every article for tracked company names and surface
    # matches at the top of the doc before the main news table. The '_watchlist_matches'
    # field is only used for debugging — it isn't rendered in the Word output.
    if watchlist:
        watchlist_hits = []
        for article in articles:
            matches = _check_watchlist(article, watchlist)
            if matches:
                article['_watchlist_matches'] = matches
                watchlist_hits.append(article)
        if watchlist_hits:
            print(f"Watchlist: {len(watchlist_hits)} article(s) matched")
            create_watchlist_section(doc, watchlist_hits, translate, claude_key, chinese_only)
            _add_horizontal_rule(doc)
        else:
            print("Watchlist: no matches found this period")

    # Create news table (grouped for deeptech, flat for others)
    print("Creating news table...")
    if funding_topic == 'deeptech':
        create_grouped_deeptech_table(doc, articles, chinese_only, translate, claude_key)
    else:
        create_news_table(doc, articles, max_articles, translate, claude_key, chinese_only)

    # Two-source funding pipeline:
    #   1. Claude extracts from the articles we already collected — fast, cheap (~$0.02),
    #      and high fidelity because these articles were hand-curated by our sources.
    #   2. OpenAI web search supplements with any funding events that weren't covered
    #      by TechCrunch / TLDR (smaller raises, non-English coverage, etc.).
    # Both lists are merged by _merge_funding_events(), keeping the richer entry per company.
    # Claude extraction is AI-only for now; deeptech still relies solely on OpenAI web search.
    topic_label = "Deeptech" if funding_topic == "deeptech" else "AI"
    all_funding_events = []

    if claude_key and funding_topic == 'ai':
        print("Extracting funding events from collected articles (Claude)...")
        article_events = extract_funding_with_claude(claude_key, articles)
        all_funding_events.extend(article_events)

    if openai_key:
        print(f"Supplementing with {topic_label} funding web search (ChatGPT)...")
        web_events = extract_funding_with_openai(openai_key, start_date, end_date, funding_topic, claude_key)
        all_funding_events = _merge_funding_events(all_funding_events, web_events)
        print(f"  Total after merge: {len(all_funding_events)} funding events")
    elif not all_funding_events:
        print("  WARNING: OPENAI_API_KEY not set and no Claude events found, skipping funding section")

    if all_funding_events:
        heading_map = {"AI": "AI 融资动态", "Deeptech": "深科技融资动态"}
        create_funding_table(doc, all_funding_events, heading=heading_map.get(topic_label, f"{topic_label} 融资动态"))

    # Save document
    os.makedirs(output_dir, exist_ok=True)
    filename = f'{output_prefix}_{start_date.replace("-", "")}_{end_date.replace("-", "")}.docx'
    filepath = os.path.join(output_dir, filename)

    doc.save(filepath)

    print(f"\n✓ Document saved to {filepath}")
    return filepath


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Generate AI News Word document')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--articles', default='.tmp/summarized_articles.json', help='Summarized articles file')
    parser.add_argument('--output_dir', default='output', help='Output directory')
    parser.add_argument('--max', type=int, default=None, help='Maximum articles to include (default: all)')
    parser.add_argument('--translate', action='store_true', help='Add Chinese translation using ChatGPT')
    parser.add_argument('--chinese-only', action='store_true', help='Output Chinese summary only (no English), for pre-summarized Chinese articles')
    parser.add_argument('--output-prefix', default='AI_News', help='Output filename prefix (default: AI_News)')
    parser.add_argument('--funding-topic', choices=['ai', 'deeptech'], default='ai',
                        help='Funding search topic: ai (default) or deeptech')
    parser.add_argument('--doc-title', default=None, help='Document title (default: AI News Report)')
    parser.add_argument('--min-signal', type=int, default=1, metavar='N',
                        help='Minimum relevance score 1-5 to include in news table (default: 1 = all). '
                             'Use 3 to drop low-value articles, 4 for high-signal-only.')
    parser.add_argument('--watchlist', default='watchlist.txt', metavar='FILE',
                        help='Path to watchlist file (default: watchlist.txt). '
                             'One company name per line. Matching articles appear in a highlights section.')
    args = parser.parse_args()

    generate_word_doc(
        args.start_date,
        args.end_date,
        args.articles,
        args.output_dir,
        args.max,
        args.translate,
        args.chinese_only,
        args.output_prefix,
        args.funding_topic,
        args.doc_title,
        args.min_signal,
        args.watchlist,
    )


if __name__ == "__main__":
    main()
