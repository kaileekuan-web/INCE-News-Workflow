#!/usr/bin/env python3
"""
Fetch full article content and generate bullet point summaries using AI

Supports:
- Claude (Anthropic) - claude-sonnet-4-20250514
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


SUMMARY_PROMPT_EN = (
    "You are a news summarizer. Create a concise bullet point summary (2-4 bullets) "
    "highlighting the key information. Focus on what happened, who is involved, and why it matters.\n\n"
    "Article to summarize:\n{text}\n\n"
    "Provide only the bullet points, no introduction or conclusion."
)

SUMMARY_PROMPT_ZH = (
    "你是一位AI行业分析师。请用简洁流畅的中文段落（2-4句话）总结以下文章，"
    "内容涵盖：发生了什么、涉及哪些方、事件意义，以及对行业或市场的潜在影响。"
    "将影响与启示自然融入叙述，不要单独列出，也不要使用列表或要点形式。"
    "摘要须直接从事实开始，不要使用「本文报道」、「文章介绍」等引导语。\n\n"
    "待总结的文章：\n{text}\n\n"
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
    "4. summary（中文摘要）：2-4句自然段，涵盖发生了什么、涉及方、为何重要。"
    "直接从事实开始，不用「本文报道」等引导语。\n\n"
    "仅返回JSON，不含其他文字：\n"
    "{{\"category\": \"模型与研究\", \"relevance\": 4, \"vc_signal\": \"research\", \"summary\": \"...\"}}\n\n"
    "文章内容：\n{text}"
)

# Consumer mode: classify + summarize in one call, returns JSON
SUMMARY_PROMPT_CONSUMER = """你是一位消费科技行业分析师。请分析以下文章，完成两项任务：

1. 将文章分类为以下两类之一：
   - "行业动态"：关于新产品发布、行业趋势、品牌动态、市场变化等
   - "融资新闻"：关于初创公司融资轮次、Pre-IPO、战略投资、收购等投资相关内容

2. 根据类别生成中文摘要（1-2段自然段，不使用列表或要点）：
   - 行业动态：概括事件核心内容、涉及方及市场意义
   - 融资新闻：详细介绍以下内容（如文章中有提及）：公司主营产品及业务概况、创始人背景、融资金额与阶段、是否已有营收、公司规模（员工数/用户数/GMV等）

   重要：摘要必须直接从新闻内容开始，不要使用"本文报道了"、"文章介绍了"、"本文介绍"、"这篇文章"等引导语。直接陈述事实。

仅返回如下JSON格式，不要包含任何其他内容：
{{"category": "行业动态", "summary": "..."}}

待分析的文章：
{text}"""


def get_summary_prompt(text: str, language: str = 'en', consumer: bool = False,
                       categorize: bool = False) -> str:
    """Return the summarization prompt for the given language/mode."""
    if categorize:
        return SUMMARY_PROMPT_AI.format(text=text)
    if consumer:
        return SUMMARY_PROMPT_CONSUMER.format(text=text)
    template = SUMMARY_PROMPT_ZH if language == 'zh' else SUMMARY_PROMPT_EN
    return template.format(text=text)


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
            max_tokens=200
        )

        summary = response.choices[0].message.content.strip()
        return summary

    except Exception as e:
        print(f"  WARNING: Summarization failed: {e}")
        # Fallback to description
        return description if description else title


def generate_summary_claude(api_key: str, title: str, description: str, content: str,
                            language: str = 'en', consumer: bool = False,
                            categorize: bool = False) -> dict:
    """
    Generate summary using Claude API.

    - Default: returns {'summary': str}
    - consumer mode: returns {'category': str, 'summary': str}
    - categorize mode: returns {'category': str, 'relevance': int, 'summary': str}

    Args:
        api_key: Anthropic API key
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

    try:
        url = "https://api.anthropic.com/v1/messages"

        headers = {
            'Content-Type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': '2023-06-01'
        }

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 600,
            "messages": [
                {
                    "role": "user",
                    "content": get_summary_prompt(text_to_summarize, language, consumer, categorize)
                }
            ]
        }

        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()

        result = response.json()

        if 'content' not in result or not result['content']:
            print(f"  WARNING: Unexpected Claude response format")
            return {'summary': fallback_summary}

        text = result['content'][0]['text'].strip()

        if consumer:
            # Extract category and summary via regex — more robust than json.loads
            # because Claude sometimes includes unescaped quotes or literal newlines
            # inside the JSON string values.
            try:
                # Strip code fences if present
                json_text = re.sub(r'^```(?:json)?\s*\n?|\n?```$', '', text.strip()).strip()

                # Extract category (always one of two known values)
                cat_match = re.search(r'"category"\s*:\s*"(行业动态|融资新闻)"', json_text)
                category = cat_match.group(1) if cat_match else '行业动态'

                # Extract summary: everything between `"summary": "` and the final `"}`
                sum_marker = '"summary": "'
                sum_pos = json_text.find(sum_marker)
                if sum_pos != -1:
                    summary_raw = json_text[sum_pos + len(sum_marker):]
                    # Strip trailing closing quote and brace
                    summary_raw = re.sub(r'"\s*\}?\s*$', '', summary_raw).strip()
                    # Convert escaped \n sequences to real newlines for paragraph spacing
                    summary_text = summary_raw.replace('\\n', '\n')
                else:
                    summary_text = fallback_summary

                return {'category': category, 'summary': summary_text}

            except Exception:
                print(f"  WARNING: Could not parse consumer response, defaulting to 行业动态")
                return {'category': '行业动态', 'summary': fallback_summary}

        if categorize:
            try:
                # Strip markdown code fences Claude sometimes wraps around JSON
                json_text = re.sub(r'^```(?:json)?\s*\n?|\n?```$', '', text.strip()).strip()

                # Use regex rather than json.loads because Claude occasionally includes
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

                return {'category': category, 'relevance': relevance, 'vc_signal': vc_signal, 'summary': summary_text}

            except Exception:
                print(f"  WARNING: Could not parse categorize response, using defaults")
                return {'category': '其他', 'relevance': 3, 'vc_signal': 'other', 'summary': fallback_summary}

        return {'summary': text}

    except Exception as e:
        print(f"  WARNING: Summarization failed: {e}")
        return {'summary': fallback_summary}


def estimate_cost_claude(num_articles: int) -> float:
    """
    Estimate Claude summarization cost

    Args:
        num_articles: Number of articles to summarize

    Returns:
        Estimated cost in USD
    """
    # Rough estimate: 2000 tokens input + 150 tokens output per article
    input_tokens = num_articles * 2000
    output_tokens = num_articles * 150

    # Claude Sonnet pricing (per million tokens)
    input_cost = (input_tokens / 1_000_000) * 3.00
    output_cost = (output_tokens / 1_000_000) * 15.00

    return input_cost + output_cost


def estimate_cost_gemini(num_articles: int) -> float:
    """Gemini is FREE up to 1500 requests/day"""
    return 0.0


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
        provider: 'claude', 'gemini', or 'openai'
        skip_confirm: Skip cost confirmation prompt
        language: Summary output language ('en' for English, 'zh' for Chinese)
        consumer: Consumer mode — classify articles into 行业动态/融资新闻 and generate
                  category-appropriate Chinese summaries (Claude only)
    """
    load_dotenv()

    # Check for API key based on provider
    if provider == 'claude':
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not found in environment")
            print("Get your API key at: https://console.anthropic.com/")
            print("Locally: add ANTHROPIC_API_KEY=your_key to your .env file")
            print("On Railway: add ANTHROPIC_API_KEY in the Variables tab, then redeploy")
            sys.exit(1)
        client = None
        print(f"Using Claude (claude-sonnet-4-20250514) for summarization")
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
    if provider in ('claude', 'openai') and estimated_cost > 2.0 and not skip_confirm:
        response = input(f"\nSummarization will cost approximately ${estimated_cost:.2f}. Continue? (y/n): ")
        if response.lower() != 'y':
            print("Summarization cancelled")
            sys.exit(0)

    # Process articles — start with already-done ones so output file stays complete
    print(f"\nProcessing {len(remaining)} articles...")
    summarized_articles = list(already_done.values())

    skipped_count = 0
    for i, article in enumerate(remaining, 1):
        print(f"[{i}/{len(remaining)}] {article.get('title', 'Untitled')[:60]}...")

        url = article.get('url', '')

        # Skip X.com/Twitter links - can't scrape them, use description instead
        if is_x_twitter_link(url):
            print(f"  (Skipping X/Twitter link - using description)")
            article['summary'] = article.get('description', article.get('title', ''))
            article['full_content_fetched'] = False
            article['skipped_x_twitter'] = True
            summarized_articles.append(article)
            skipped_count += 1
            continue

        # Fetch full content unless skip_fetch is True
        content = ""
        if not skip_fetch:
            if url:
                content = fetch_article_content(url)
                time.sleep(0.5)  # Rate limiting

        # Generate summary
        if provider == 'claude':
            result = generate_summary_claude(
                api_key,
                article.get('title', ''),
                article.get('description', ''),
                content,
                language,
                consumer,
                categorize,
            )
            time.sleep(0.5)  # Small delay for Claude
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
    parser.add_argument('--language', choices=['en', 'zh'], default='en',
                        help='Summary output language: en=English (default), zh=Chinese')
    parser.add_argument('--consumer', action='store_true',
                        help='Consumer mode: classify articles into 行业动态/融资新闻 and generate '
                             'category-appropriate Chinese summaries (Claude only)')
    parser.add_argument('--no-categorize', dest='categorize', action='store_false',
                        help='Disable AI news categorization (on by default). '
                             'Categorization classifies into 6 categories and scores relevance 1-5.')
    parser.set_defaults(categorize=True)
    args = parser.parse_args()

    summarize_articles(args.input, args.output, args.max, args.skip_fetch, args.provider,
                       args.yes, args.language, args.consumer, args.categorize)


if __name__ == "__main__":
    main()
