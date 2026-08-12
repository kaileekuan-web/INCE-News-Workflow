#!/usr/bin/env python3
"""
Fetch full article content and generate bullet point summaries using AI

Supports:
- Claude (Anthropic API) - default, claude-opus-5
- Google Gemini (FREE - 1500 requests/day)
- OpenAI GPT (Paid - requires payment method)

Strategy:
1. Load articles from classified_articles.json
2. Fetch full content from each URL (optional)
3. Use AI to generate concise bullet point summaries
4. Add summaries to articles

Input: .tmp/classified_articles.json
Output: .tmp/summarized_articles.json
"""

import os
import sys
import json
import re
import argparse
import time
from typing import List, Dict, Any
from dotenv import load_dotenv

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Run: pip install requests beautifulsoup4")
    sys.exit(1)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import call_claude
from tools.grounding import unsupported_figures


def is_x_twitter_link(url: str, timeout: int = 5) -> bool:
    """
    Check if URL points to X.com or Twitter (directly or via redirect).
    These sites block scraping, so we skip them.

    Args:
        url: URL to check
        timeout: Request timeout for following redirects

    Returns:
        True if URL is or redirects to X.com/Twitter
    """
    from urllib.parse import unquote

    # First check the URL itself (decoded)
    decoded_url = unquote(url).lower()
    x_patterns = ['x.com/', 'twitter.com/', '//x.com', '//twitter.com']
    if any(pattern in decoded_url for pattern in x_patterns):
        return True

    # If it's a redirect link, follow it to get the final destination
    if 'links.tldrnewsletter.com' in url or 'tracking.tldrnewsletter.com' in url:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            }
            # Use GET request with stream=True to follow redirects without downloading full content
            response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
            final_url = response.url.lower()
            response.close()  # Close without reading body
            if any(pattern in final_url for pattern in x_patterns):
                return True
        except Exception:
            # If we can't resolve, assume it's not X.com
            pass

    return False


def fetch_article_content(url: str, timeout: int = 10) -> str:
    """
    Fetch full article content from URL

    Args:
        url: Article URL
        timeout: Request timeout in seconds

    Returns:
        Article content as text
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')

        # Remove script and style elements
        for script in soup(['script', 'style', 'nav', 'footer', 'header']):
            script.decompose()

        # Try to find article content
        # Common article containers
        article = None
        for selector in ['article', '[role="main"]', '.article-content', '.post-content', 'main']:
            article = soup.select_one(selector)
            if article:
                break

        if article:
            text = article.get_text()
        else:
            # Fallback to body
            text = soup.body.get_text() if soup.body else soup.get_text()

        # Clean up whitespace
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = '\n'.join(chunk for chunk in chunks if chunk)

        return text[:8000]  # Limit to ~8000 chars to avoid token limits

    except Exception as e:
        print(f"  WARNING: Could not fetch {url}: {e}")
        return ""


# ── Grounding ─────────────────────────────────────────────────────────────────
# Attached to every summarization prompt. The two rules that matter most:
#
#   1. No outside knowledge. The model knows a great deal about OpenAI and
#      Anthropic, and without this it will helpfully blend that in — producing
#      sentences that read as reporting but trace to nothing in the source.
#   2. Length follows the source. A 200-character tweet asked for a 6-sentence
#      summary has to invent four sentences of significance to fill the space.
#      That is the single largest driver of fabricated "why it matters" claims,
#      and it is a prompt problem, not a model problem — see _length_rule().
GROUNDING_RULES_ZH = (
    "【事实约束——严格遵守】\n"
    "1. 只能使用下方原文中明确出现的信息。禁止补充你已知的背景知识、行业常识、"
    "公司历史或对后续发展的推测。\n"
    "2. 数字、金额、估值、比例、日期、公司名、产品名、人名、机构名必须与原文一致。"
    "原文没有的数字，一律不得出现在摘要中。\n"
    "3. 原文信息少时，摘要就写短。宁可只写两句真实内容，也不要用推测、泛泛而谈或"
    "行业套话把篇幅凑满。\n"
    "4. 不确定或原文未写明的内容直接省略。不要写「可能」「预计」「有望」这类推测。\n"
    "5. 不要用任何句子说明原文缺了什么——「原文未提及」「未说明具体内容」「未透露细节」"
    "「具体信息不详」等表述一律不要出现。原文只写了一句话，摘要就只写那一句的事实。\n"
)

GROUNDING_RULES_EN = (
    "STRICT GROUNDING RULES:\n"
    "1. Use only information explicitly present in the source below. Do not add "
    "background knowledge, industry context, company history, or predictions.\n"
    "2. Every figure, amount, valuation, percentage, date and name must match the "
    "source exactly. Never introduce a number the source does not contain.\n"
    "3. When the source is thin, write less. Two true sentences beat six padded ones.\n"
    "4. Omit anything uncertain rather than hedging it, and never write sentences "
    "about what the source failed to mention.\n"
)


def _length_rule(text: str) -> str:
    """
    Summary length instruction scaled to how much source material there is.

    A tweet supports one or two sentences of fact. Asking for the same 4-6
    sentences we want from a full article is an instruction to pad, and padding
    is where invented significance comes from.
    """
    n = len(text or '')
    if n < 400:
        return "1-2句"
    if n < 1200:
        return "2-4句"
    return "4-6句"


SUMMARY_PROMPT_EN = (
    "You are a news summarizer. Create a concise bullet point summary (2-4 bullets) "
    "highlighting the key information. Focus on what happened and who is involved.\n\n"
    + GROUNDING_RULES_EN +
    "\nArticle to summarize:\n{text}\n\n"
    "Provide only the bullet points, no introduction or conclusion."
)

SUMMARY_PROMPT_ZH = (
    "你是一位AI行业分析师。请用简洁流畅的中文段落（{length_rule}）总结以下文章，"
    "内容涵盖：发生了什么、涉及哪些方，以及原文本身已经说明的意义或影响。"
    "将影响与启示自然融入叙述，不要单独列出，也不要使用列表或要点形式。"
    "摘要须直接从事实开始，不要使用「本文报道」、「文章介绍」等引导语。\n\n"
    + GROUNDING_RULES_ZH +
    "\n待总结的文章：\n{text}\n\n"
    "只输出中文摘要段落，不需要标题或其他说明。"
)

# AI news mode: classify + score + vc_signal + Chinese summary in one call, returns JSON.
# Reframed from "AI practitioner value" to "VC investment value" so the relevance scores
# weight what actually matters for investment decisions: funding rounds, founder moves,
# market-shifting partnerships — not just technically interesting news.
SUMMARY_PROMPT_AI = (
    "你是一位AI行业分析师，服务于专注AI领域的风险投资机构。请分析以下文章，用JSON格式返回四项内容：\n\n"
    "1. category（类别）— 选择以下之一：\n"
    "   \"模型与研究\"：新模型发布、研究论文、技术突破、基准测试\n"
    "   \"产品与应用\"：新产品功能、应用场景、用户工具\n"
    "   \"大科技公司\"：Google、Apple、Meta、Microsoft、Amazon、NVIDIA、xAI等大型科技公司动态\n"
    "   \"政策与安全\"：AI监管、安全研究、伦理讨论、政府政策\n"
    "   \"行业动态\"：公司战略、合作、市场趋势、商业新闻\n"
    "   \"其他\"：不属于以上类别\n\n"
    "2. relevance（投资价值评分，1-5整数）——从VC视角衡量该新闻对投资决策的参考价值：\n"
    "   5 = 必读：融资轮次（A轮以上大额）、重大收购、技术颠覆性突破、监管重大政策变化\n"
    "   4 = 重要：新公司/产品进入市场、重要战略合作、知名创始人新动向、市场格局变化\n"
    "   3 = 值得关注：渐进式产品更新、行业趋势分析、中小额融资、有参考价值但非紧迫\n"
    "   2 = 一般：边缘公司小动作、重复性更新、话题缺乏新意\n"
    "   1 = 低价值：营销内容、高度推测性报道、与AI投资关联较弱\n\n"
    "3. vc_signal（VC信号类型）— 选择以下之一：\n"
    "   \"funding\"：融资轮次、收购、IPO、战略投资等资金事件\n"
    "   \"product\"：新产品发布、重大功能更新、技术突破\n"
    "   \"partnership\"：战略合作、重要客户公告、生态集成\n"
    "   \"hire\"：重要高管任命、创始团队变动、关键人才流动\n"
    "   \"regulatory\"：监管政策、法规变化、政府动向\n"
    "   \"research\"：研究成果、学术发现、技术验证\n"
    "   \"other\"：不属于以上类别\n\n"
    "4. summary（中文摘要）：{length_rule}自然段，涵盖发生了什么、涉及方，以及原文本身已说明的意义。"
    "直接从事实开始，不用「本文报道」等引导语。\n\n"
    + GROUNDING_RULES_ZH +
    "\n仅返回JSON，不含其他文字：\n"
    "{{\"category\": \"模型与研究\", \"relevance\": 4, \"vc_signal\": \"research\", \"summary\": \"...\"}}\n\n"
    "文章内容：\n{text}"
)

# Consumer mode: classify + summarize in one call, returns JSON
SUMMARY_PROMPT_CONSUMER = """你是一位资深消费科技行业分析师，服务于投资机构的投研团队。请分析以下文章，完成两项任务：

1. 将文章分类为以下两类之一：
   - "行业动态"：关于新产品发布、行业趋势、品牌动态、市场变化等
   - "融资新闻"：关于初创公司融资轮次、Pre-IPO、战略投资、收购等投资相关内容

2. 根据类别生成中文摘要（1-2段自然段，不使用列表或要点）：
   - 行业动态：概括事件核心内容、涉及方及市场意义，包含具体数字（销量、增长率、市场规模等，如文章提及）
   - 融资新闻：必须逐项检查并纳入以下要素（文章中提及则写入，未提及则不要编造或用"未披露"敷衍——直接省略该要素，不要生硬提及"未提及"）：
     a) 公司核心产品/业务是什么，解决什么问题、目标用户是谁
     b) 创始人/核心团队背景（曾任职公司、过往创业经历、专业背景）
     c) 本轮融资金额、轮次、估值（如有）
     d) 领投/跟投机构名称
     e) 公司经营现状：是否盈利、营收规模、用户/GMV/门店数等经营指标
     f) 本轮资金的具体用途（扩张、研发、供应链等，如文章提及）

要求：
- 只写文章中确实包含的事实，绝不编造或推测数字、机构名、人名
- 优先使用文章中的具体数字和专有名词，避免"大量"、"显著增长"等模糊表述
- 摘要必须直接从新闻内容开始，不要使用"本文报道了"、"文章介绍了"、"本文介绍"、"这篇文章"等引导语，直接陈述事实
- 语言专业、信息密度高，避免空洞的行业评价句（如"这体现了行业的蓬勃发展"）
- 严禁出现"未提及"、"未披露"、"没有详细说明"、"文章未透露"等描述缺失信息的句子——如果某个要素文章没写，就完全不提这个要素，绝不能写一句话专门说明它缺失。摘要只能由文章中确实存在的信息组成，哪怕整篇文章信息很少，摘要也只写那几条真实信息，不必凑够所有要素

示例（融资新闻，摘要风格参考）：
"XX科技完成5000万元A轮融资，由红杉中国领投，老股东XX资本跟投。公司创始人张三曾任职于字节跳动电商部门，负责供应链管理；联合创始人李四此前在SHEIN从事产品设计。XX科技主营面向Z世代的快时尚订阅平台，目前月活用户超200万，GMV同比增长150%，本轮资金将主要用于扩大海外仓网络和自建供应链。"

仅返回如下JSON格式，不要包含任何其他内容：
{{"category": "行业动态", "summary": "..."}}

待分析的文章：
{text}"""

# Second-pass editor prompt: reviews the draft against the original text and
# tightens it. Running a cheap local model twice (draft, then critique+rewrite)
# closes most of the quality gap vs. a single call to a stronger hosted model —
# it catches vague filler, missed concrete details, and hedging the draft prompt
# alone doesn't reliably prevent.
CONSUMER_REFINE_PROMPT = """你是消费科技投研团队的资深编辑，正在审核一位初级分析师写的中文摘要。请对照原文，检查并改进这份摘要，输出最终版本。

检查清单：
1. 摘要中的每个数字、机构名、人名是否都能在原文中找到依据？删除任何原文没有明确支持的内容。
2. 原文中是否有摘要遗漏的关键信息（融资金额、投资方、创始人背景、经营数据等）？如有，补充进去。
3. 是否有模糊、空洞的表述（如"引发广泛关注"、"显著提升"）？替换为原文中的具体事实，如原文本身也无具体数字，则直接删除这类空话。
4. 分类（category）是否准确？
5. 摘要中是否出现"未提及"、"未披露"、"没有详细说明"、"文章未透露"等描述信息缺失的字眼？如有，只删除这几个字/这个短句，绝不能删掉整句话——句子里其他真实信息必须保留，删除后把剩下部分自然连接成完整句子。

   示例：
   修改前："XX完成新一轮战略融资，投资方为国家绿色发展基金，具体金额未提及。资金将用于扩大产能及研发投入。"
   修改后："XX完成新一轮战略融资，投资方为国家绿色发展基金，资金将用于扩大产能及研发投入。"
   （只删掉了"，具体金额未提及"这几个字，投资方和资金用途这两条真实信息完整保留并连接成一句）

   摘要只应包含原文中真实存在的信息，不应评论原文缺了什么；但绝不能因为删除"未提及"字样而连带丢失同一句里的其他真实事实。

原文：
{original_text}

初级分析师的草稿：
{draft_json}

仅返回改进后的最终JSON，格式与草稿一致，不要包含任何其他文字：
{{"category": "行业动态", "summary": "..."}}"""


def get_summary_prompt(text: str, language: str = 'en', consumer: bool = False,
                       categorize: bool = False) -> str:
    """Return the summarization prompt for the given language/mode."""
    length_rule = _length_rule(text)
    if categorize:
        return SUMMARY_PROMPT_AI.format(text=text, length_rule=length_rule)
    if consumer:
        return SUMMARY_PROMPT_CONSUMER.format(text=text)
    if language == 'zh':
        return SUMMARY_PROMPT_ZH.format(text=text, length_rule=length_rule)
    return SUMMARY_PROMPT_EN.format(text=text)


def generate_summary_gemini(api_key: str, title: str, description: str, content: str, language: str = 'en') -> str:
    """
    Generate bullet point summary using Google Gemini API

    Args:
        api_key: Google Gemini API key
        title: Article title
        description: Article description
        content: Full article content
        language: Output language ('en' or 'zh')

    Returns:
        Bullet point summary
    """
    # If content is empty, use title + description
    if not content or len(content) < 100:
        text_to_summarize = f"{title}\n\n{description}"
    else:
        text_to_summarize = f"{title}\n\n{description}\n\n{content}"

    try:
        # Use Gemini REST API
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"

        headers = {'Content-Type': 'application/json'}

        payload = {
            "contents": [{
                "parts": [{
                    "text": get_summary_prompt(text_to_summarize, language)
                }]
            }],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1000  # Increased to account for thinking tokens
            }
        }

        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        result = response.json()

        if 'candidates' in result and len(result['candidates']) > 0:
            summary = result['candidates'][0]['content']['parts'][0]['text'].strip()
            return summary
        else:
            print(f"  WARNING: Unexpected Gemini response format")
            return description if description else title

    except Exception as e:
        print(f"  WARNING: Summarization failed: {e}")
        # Fallback to description
        return description if description else title


def generate_summary_openai(client, title: str, description: str, content: str, language: str = 'en') -> str:
    """
    Generate bullet point summary using OpenAI

    Args:
        client: OpenAI client
        title: Article title
        description: Article description
        content: Full article content
        language: Output language ('en' or 'zh')

    Returns:
        Bullet point summary
    """
    # If content is empty, use title + description
    if not content or len(content) < 100:
        text_to_summarize = f"{title}\n\n{description}"
    else:
        text_to_summarize = f"{title}\n\n{description}\n\n{content}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": get_summary_prompt(text_to_summarize, language)
                }
            ],
            temperature=0.3,
            max_tokens=500
        )

        summary = response.choices[0].message.content.strip()
        return summary

    except Exception as e:
        print(f"  WARNING: Summarization failed: {e}")
        # Fallback to description
        return description if description else title


def _parse_category_summary_json(text: str, fallback_summary: str) -> dict:
    """
    Shared regex-based parser for {"category": ..., "summary": ...} responses.
    Regex rather than json.loads because the model sometimes includes unescaped
    quotes or literal newlines inside the JSON string values.
    """
    try:
        json_text = re.sub(r'^```(?:json)?\s*\n?|\n?```$', '', text.strip()).strip()

        cat_match = re.search(r'"category"\s*:\s*"(行业动态|融资新闻)"', json_text)
        category = cat_match.group(1) if cat_match else '行业动态'

        sum_marker = '"summary": "'
        sum_pos = json_text.find(sum_marker)
        if sum_pos != -1:
            summary_raw = json_text[sum_pos + len(sum_marker):]
            summary_raw = re.sub(r'"\s*\}?\s*$', '', summary_raw).strip()
            summary_text = summary_raw.replace('\\n', '\n')
        else:
            summary_text = fallback_summary

        return {'category': category, 'summary': summary_text}

    except Exception:
        print(f"  WARNING: Could not parse consumer response, defaulting to 行业动态")
        return {'category': '行业动态', 'summary': fallback_summary}


REPAIR_PROMPT = (
    "下面这段摘要中，有些数字在原文里找不到依据。请修正后重新输出。\n\n"
    "找不到依据的数字：{figures}\n\n"
    "修改要求：\n"
    "- 删除这些数字，或改写成原文确实支持的说法。若删掉数字后整句仍成立，保留该句其余部分。\n"
    "- 其他内容一字不改。不要重写风格、不要补充新信息、不要加入原文没有的数字。\n"
    "- 不要写「原文未提及」之类说明缺失的句子。\n\n"
    "原文：\n{source}\n\n"
    "待修正的摘要：\n{summary}\n\n"
    "{output_instruction}"
)


def _repair_ungrounded(summary: str, source: str, missing: list,
                       as_json: bool = False) -> str:
    """
    One targeted repair pass for figures the source doesn't support.

    This is deliberately not a general "check your work" pass — those make the
    model second-guess correct output and cost a call on every article. It fires
    only when the arithmetic check in tools/grounding.py found a specific
    mismatch, and it is told exactly which figures to fix.
    """
    output_instruction = (
        "仅返回修正后的JSON，格式与输入一致，不要包含其他文字。" if as_json
        else "只输出修正后的摘要正文，不要其他说明。"
    )
    return call_claude(
        REPAIR_PROMPT.format(
            figures='、'.join(missing),
            source=source,
            summary=summary,
            output_instruction=output_instruction,
        ),
        max_tokens=4096,
    )


def _ground_summary(summary: str, source: str, label: str = '') -> tuple:
    """
    Verify every figure in `summary` traces to `source`; repair once if not.

    Returns (summary, unresolved_figures). A non-empty second element means the
    repair pass did not fully clear the problem — the caller records it on the
    article so a bad figure is visible in the data rather than only in the doc.
    """
    if not summary:
        return summary, []

    missing = unsupported_figures(summary, source)
    if not missing:
        return summary, []

    print(f"  GROUNDING: {label}unsupported figure(s) {missing} — repairing")
    repaired = _repair_ungrounded(summary, source, missing)
    if not repaired:
        return summary, missing

    still_missing = unsupported_figures(repaired, source)
    if len(still_missing) < len(missing):
        if still_missing:
            print(f"  GROUNDING: {label}still unsupported after repair: {still_missing}")
        return repaired, still_missing

    # Repair didn't help — keep the original rather than a rewrite that
    # introduced its own problems, and flag it.
    return summary, missing


def generate_summary_claude(title: str, description: str, content: str,
                            language: str = 'en', consumer: bool = False,
                            categorize: bool = False) -> dict:
    """
    Generate summary using Claude.

    - Default: returns {'summary': str}
    - consumer mode: returns {'category': str, 'summary': str}
    - categorize mode: returns {'category': str, 'relevance': int, 'summary': str}

    Args:
        title: Article title
        description: Article description
        content: Full article content
        language: Output language ('en' or 'zh')
        consumer: Consumer classification+summary mode
        categorize: AI news categorize+score+summary mode
    """
    if not content or len(content) < 100:
        text_to_summarize = f"{title}\n\n{description}"
    else:
        text_to_summarize = f"{title}\n\n{description}\n\n{content}"

    fallback_summary = description if description else title

    text = call_claude(get_summary_prompt(text_to_summarize, language, consumer, categorize))

    if not text:
        return {'summary': fallback_summary}

    if consumer:
        draft = _parse_category_summary_json(text, fallback_summary)

        # The self-critique second pass runs only when explicitly asked for
        # (CONSUMER_REFINE=1). It existed to close the quality gap between a
        # small local model and a hosted one — that gap is what we just paid to
        # remove, and asking Claude to re-check its own draft mostly buys
        # over-verification at double the per-article cost. Kept behind a flag
        # because it is cheap to re-enable if a run reads thin.
        if os.getenv('CONSUMER_REFINE') == '1':
            refine_prompt = CONSUMER_REFINE_PROMPT.format(
                original_text=text_to_summarize,
                draft_json=json.dumps(draft, ensure_ascii=False),
            )
            refined_text = call_claude(refine_prompt)
            if refined_text:
                refined = _parse_category_summary_json(refined_text, draft['summary'])
                # Guard against a degenerate refine pass (empty/truncated output)
                if refined.get('summary') and len(refined['summary']) >= 20:
                    draft = refined

        draft['summary'], flags = _ground_summary(draft['summary'], text_to_summarize)
        if flags:
            draft['grounding_flags'] = flags
        return draft

    if categorize:
        try:
            # Strip markdown code fences the model sometimes wraps around JSON
            json_text = re.sub(r'^```(?:json)?\s*\n?|\n?```$', '', text.strip()).strip()

            # Use regex rather than json.loads because the model occasionally includes
            # unescaped quotes or newlines inside summary strings that break the parser.
            cat_match = re.search(r'"category"\s*:\s*"([^"]+)"', json_text)
            rel_match = re.search(r'"relevance"\s*:\s*([1-5])', json_text)
            vc_match = re.search(r'"vc_signal"\s*:\s*"([^"]+)"', json_text)
            sum_marker = '"summary": "'
            sum_pos = json_text.find(sum_marker)

            category = cat_match.group(1) if cat_match else '其他'
            relevance = int(rel_match.group(1)) if rel_match else 3
            vc_signal = vc_match.group(1) if vc_match else 'other'

            if sum_pos != -1:
                summary_raw = json_text[sum_pos + len(sum_marker):]
                summary_text = re.sub(r'"\s*\}?\s*$', '', summary_raw).strip()
            else:
                summary_text = fallback_summary

            summary_text, flags = _ground_summary(summary_text, text_to_summarize)
            result = {'category': category, 'relevance': relevance,
                      'vc_signal': vc_signal, 'summary': summary_text}
            if flags:
                result['grounding_flags'] = flags
            return result

        except Exception:
            print(f"  WARNING: Could not parse categorize response, using defaults")
            return {'category': '其他', 'relevance': 3, 'vc_signal': 'other', 'summary': fallback_summary}

    text, flags = _ground_summary(text, text_to_summarize)
    result = {'summary': text}
    if flags:
        result['grounding_flags'] = flags
    return result


def estimate_cost_gemini(num_articles: int) -> float:
    """Gemini is FREE up to 1500 requests/day"""
    return 0.0


def estimate_cost_claude(num_articles: int) -> float:
    """
    Estimate Claude summarization cost.

    Per article: a fetched article capped at ~8000 chars plus the prompt is
    roughly 2500 input tokens; the Chinese summary plus low-effort thinking is
    roughly 700 output tokens. Priced at Opus 5 list rates ($5 / $25 per MTok),
    so this over-estimates on Sonnet or Haiku.
    """
    input_tokens = num_articles * 2500
    output_tokens = num_articles * 700

    return (input_tokens / 1_000_000) * 5.0 + (output_tokens / 1_000_000) * 25.0


def estimate_cost_openai(num_articles: int) -> float:
    """
    Estimate OpenAI translation cost

    Args:
        num_articles: Number of articles to summarize

    Returns:
        Estimated cost in USD
    """
    # Rough estimate: 2000 tokens input + 150 tokens output per article
    input_tokens = num_articles * 2000
    output_tokens = num_articles * 150

    # GPT-4o-mini pricing
    input_cost = (input_tokens / 1_000_000) * 0.15
    output_cost = (output_tokens / 1_000_000) * 0.60

    return input_cost + output_cost


def summarize_articles(input_file: str = '.tmp/classified_articles.json',
                      output_file: str = '.tmp/summarized_articles.json',
                      max_articles: int = None,
                      skip_fetch: bool = False,
                      provider: str = 'claude',
                      skip_confirm: bool = False,
                      language: str = 'en',
                      consumer: bool = False,
                      categorize: bool = False):
    """
    Main summarization function

    Args:
        input_file: Path to classified articles JSON
        output_file: Path to output summarized articles JSON
        max_articles: Maximum number of articles to process (None = all)
        skip_fetch: Skip fetching full content, use existing description only
        provider: 'claude' (default), 'gemini', or 'openai'
        skip_confirm: Skip cost confirmation prompt
        language: Summary output language ('en' for English, 'zh' for Chinese)
        consumer: Consumer mode — classify articles into 行业动态/融资新闻 and generate
                  category-appropriate Chinese summaries
    """
    load_dotenv(override=True)

    # Check for API key based on provider
    if provider == 'claude':
        from tools.utils import CLAUDE_MODEL, CLAUDE_EFFORT
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not found in .env file")
            print("Get your API key at: https://console.anthropic.com/settings/keys")
            sys.exit(1)
        client = None
        print(f"Using Claude ({CLAUDE_MODEL}, effort={CLAUDE_EFFORT}) for summarization")
    elif provider == 'gemini':
        api_key = os.getenv('GOOGLE_GEMINI_API_KEY')
        if not api_key:
            print("ERROR: GOOGLE_GEMINI_API_KEY not found in .env file")
            print("Get your FREE API key at: https://ai.google.dev/")
            print("Then add it to your .env file:")
            print("GOOGLE_GEMINI_API_KEY=your_key_here")
            sys.exit(1)
        client = None
        print(f"Using Google Gemini (FREE) for summarization")
    else:  # openai
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            print("ERROR: OPENAI_API_KEY not found in .env file")
            sys.exit(1)
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
        except ImportError:
            print("ERROR: openai package not installed. Run: pip install openai")
            sys.exit(1)
        print(f"Using OpenAI GPT-4o-mini for summarization")

    # Load articles
    print(f"Loading articles from {input_file}...")
    with open(input_file, 'r', encoding='utf-8') as f:
        articles = json.load(f)

    total_articles = len(articles)
    print(f"Loaded {total_articles} articles")

    # Limit if specified
    if max_articles and max_articles < total_articles:
        print(f"Processing only first {max_articles} articles")
        articles = articles[:max_articles]

    # Resume from previous run — load already-summarized articles by URL
    already_done: dict = {}
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            for a in existing:
                url = a.get('url', '')
                if url and a.get('summary'):
                    already_done[url] = a
            if already_done:
                print(f"  Resuming: {len(already_done)} articles already summarized, skipping them")
        except Exception:
            print("  Could not load existing progress, starting fresh")

    remaining = [a for a in articles if a.get('url', '') not in already_done]
    print(f"  Articles to process: {len(remaining)} (skipping {len(already_done)} already done)")

    # Estimate cost on remaining articles only
    if provider == 'claude':
        estimated_cost = estimate_cost_claude(len(remaining))
        print(f"\nEstimated cost: ${estimated_cost:.2f}")
    elif provider == 'gemini':
        estimated_cost = estimate_cost_gemini(len(remaining))
        print(f"\nEstimated cost: $0.00 (FREE)")
        print(f"Rate limit: 15 requests/min, 1500/day")
        if len(remaining) > 1500:
            print(f"WARNING: {len(remaining)} articles exceeds daily limit of 1500")
            sys.exit(1)
    else:
        estimated_cost = estimate_cost_openai(len(remaining))
        print(f"\nEstimated cost: ${estimated_cost:.2f}")

    # Ask for confirmation if cost is high (paid providers)
    if provider in ('openai', 'claude') and estimated_cost > 2.0 and not skip_confirm:
        response = input(f"\nSummarization will cost approximately ${estimated_cost:.2f}. Continue? (y/n): ")
        if response.lower() != 'y':
            print("Summarization cancelled")
            sys.exit(0)

    # Process articles — start with already-done ones so output file stays complete
    print(f"\nProcessing {len(remaining)} articles...")
    summarized_articles = list(already_done.values())

    skipped_count = 0
    flagged = []          # articles whose figures never traced back to the source
    for i, article in enumerate(remaining, 1):
        print(f"[{i}/{len(remaining)}] {article.get('title', 'Untitled')[:60]}...")

        url = article.get('url', '')

        # X.com/Twitter links can't be scraped. If the tweet text was already
        # captured at collection time (collect_x.py stores it in content/description),
        # summarize/score that text directly instead of fetching. Only fall back to
        # the old skip behavior when there is no captured text to work with.
        content = ""
        if is_x_twitter_link(url):
            captured = article.get('content') or article.get('description') or ''
            if captured:
                content = captured
                print(f"  (X/Twitter - using captured tweet text, no fetch)")
            else:
                print(f"  (Skipping X/Twitter link - no captured text, using description)")
                article['summary'] = article.get('description', article.get('title', ''))
                article['full_content_fetched'] = False
                article['skipped_x_twitter'] = True
                summarized_articles.append(article)
                skipped_count += 1
                continue
        # Fetch full content unless skip_fetch is True (non-X links only)
        elif not skip_fetch:
            if url:
                content = fetch_article_content(url)
                time.sleep(0.5)  # Rate limiting

        # Generate summary
        if provider == 'claude':
            result = generate_summary_claude(
                article.get('title', ''),
                article.get('description', ''),
                content,
                language,
                consumer,
                categorize,
            )
        elif provider == 'gemini':
            result = {
                'summary': generate_summary_gemini(
                    api_key,
                    article.get('title', ''),
                    article.get('description', ''),
                    content,
                    language
                )
            }
            # Respect Gemini rate limit: be conservative to avoid 429 errors
            time.sleep(6)  # 6 seconds between requests = 10 req/min (safer)
        else:
            result = {
                'summary': generate_summary_openai(
                    client,
                    article.get('title', ''),
                    article.get('description', ''),
                    content,
                    language
                )
            }
            time.sleep(0.3)  # Small delay for OpenAI

        # Add summary (and category/relevance if applicable) to article
        article['summary'] = result.get('summary', '')
        if consumer and 'category' in result:
            article['category'] = result['category']
            print(f"  Category: {result['category']}")
        if categorize and 'category' in result:
            article['category'] = result['category']
            article['relevance'] = result.get('relevance', 3)
            article['vc_signal'] = result.get('vc_signal', 'other')
            print(f"  [{result.get('relevance', 3)}/5] {result['category']} [{result.get('vc_signal', 'other')}]")
        article['full_content_fetched'] = bool(content)
        if result.get('grounding_flags'):
            # Survived a repair attempt — keep it visible in the data so a
            # reviewer can check this article before the report goes out.
            article['grounding_flags'] = result['grounding_flags']
            flagged.append((article.get('title', '')[:60], result['grounding_flags']))
        summarized_articles.append(article)

        # Save after every article so a crash doesn't lose progress
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(summarized_articles, f, ensure_ascii=False, indent=2)

        if len(summarized_articles) % 10 == 0:
            print(f"  → Summarized {len(summarized_articles)} articles so far...")

    if skipped_count > 0:
        print(f"\n✓ Summarization complete ({len(summarized_articles)} articles, {skipped_count} X/Twitter links skipped)")
    else:
        print(f"\n✓ Summarization complete ({len(summarized_articles)} articles)")

    # Grounding report. Silence here means every figure in every summary traced
    # back to its source; anything listed needs a human to look before sending.
    if flagged:
        print(f"\n⚠ GROUNDING: {len(flagged)} article(s) contain figures not found in the source:")
        for title, figures in flagged:
            print(f"    {title}... → {', '.join(figures)}")
        print("  These are marked with 'grounding_flags' in the output JSON.")
    else:
        print("\n✓ Grounding: every figure in every summary traces to its source")

    # Save output
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(summarized_articles, f, ensure_ascii=False, indent=2)

    print(f"✓ Saved to {output_file}")


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Fetch and summarize articles')
    parser.add_argument('--input', default='.tmp/classified_articles.json', help='Input file')
    parser.add_argument('--output', default='.tmp/summarized_articles.json', help='Output file')
    parser.add_argument('--max', type=int, default=None, help='Max articles to process')
    parser.add_argument('--skip-fetch', action='store_true', help='Skip fetching full content')
    parser.add_argument('--provider', choices=['claude', 'gemini', 'openai'], default='claude',
                       help='AI provider to use (default: claude)')
    parser.add_argument('--yes', '-y', action='store_true', help='Skip cost confirmation prompt')
    parser.add_argument('--language', choices=['en', 'zh'], default='zh',
                        help='Summary output language: zh=Chinese (default), en=English')
    parser.add_argument('--consumer', action='store_true',
                        help='Consumer mode: classify articles into 行业动态/融资新闻 and generate '
                             'category-appropriate Chinese summaries')
    parser.add_argument('--no-categorize', dest='categorize', action='store_false',
                        help='Disable AI news categorization (on by default). '
                             'Categorization classifies into 6 categories and scores relevance 1-5.')
    parser.set_defaults(categorize=True)
    args = parser.parse_args()

    summarize_articles(args.input, args.output, args.max, args.skip_fetch, args.provider,
                       args.yes, args.language, args.consumer, args.categorize)


if __name__ == "__main__":
    main()
