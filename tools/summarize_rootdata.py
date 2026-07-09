#!/usr/bin/env python3
"""
Summarize RootData fundraising deals using Claude API.

For each deal, fetches the source URL content and calls Claude to generate
a 2-3 sentence bilingual (Chinese-dominant) company introduction for the
"Info" column in the final Excel report.

Input:  .tmp/raw_rootdata.json (from collect_rootdata.py)
Output: .tmp/summarized_rootdata.json (same array + "info" field per deal)
"""

import os
import sys
import json
import argparse
import time

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Run: pip install requests beautifulsoup4")
    sys.exit(1)

from dotenv import load_dotenv

load_dotenv(override=True)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CLAUDE_URL = "https://api.anthropic.com/v1/messages"

MAIN_PROMPT = """你是加密货币行业分析师。根据以下信息为"{company}"写2-3句公司介绍，用于融资情报报告"Info"栏。
要求：①中文为主，自然流畅；②公司名、产品名、技术术语保留英文；③涵盖核心业务、技术亮点、团队背景（如有）；④不提融资金额/轮次（已有单独列）。
融资轮次：{round_type}，金额：{amount_raw}。
新闻摘要：{source_content}
只输出介绍段落，不加任何标题或前缀。"""

FALLBACK_PROMPT = """请用一句话简介加密/区块链公司"{company}"（中文为主，专有名词保留英文）。如信息不足，直接输出"业务详情待补充"。"""


def fetch_source_content(url: str) -> str:
    """Fetch and clean text from a source URL."""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers={"User-Agent": DESKTOP_UA}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Collapse whitespace
        import re
        text = re.sub(r"\s+", " ", text).strip()
        return text[:5000]
    except Exception as e:
        print(f"  WARNING: Could not fetch {url}: {e}")
        return ""


def generate_info(api_key: str, company: str, round_type: str,
                  amount_raw: str, source_content: str) -> str:
    """Call Claude to generate a bilingual company intro."""
    if source_content:
        prompt = MAIN_PROMPT.format(
            company=company,
            round_type=round_type or "未知",
            amount_raw=amount_raw or "未知",
            source_content=source_content,
        )
    else:
        prompt = FALLBACK_PROMPT.format(company=company)

    try:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = requests.post(CLAUDE_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if "content" in result and result["content"]:
            return result["content"][0]["text"].strip()
        print("  WARNING: Unexpected Claude response format")
        return ""
    except Exception as e:
        print(f"  WARNING: Claude call failed for {company}: {e}")
        return ""


def summarize_rootdata(input_file: str, output_file: str, api_key: str,
                       skip_confirm: bool = False) -> None:
    """Main pipeline: load deals → generate info → save incrementally."""
    with open(input_file, encoding="utf-8") as f:
        deals = json.load(f)

    if not deals:
        print("No deals found in input file.")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return

    print(f"Loaded {len(deals)} deals from {input_file}")

    # Estimate cost: ~$0.01 per deal (fetch + Claude call)
    estimated_cost = len(deals) * 0.01
    if not skip_confirm:
        print(f"Estimated cost: ~${estimated_cost:.2f} USD ({len(deals)} deals × ~$0.01)")
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # Load existing output for resume support
    existing = {}
    if os.path.exists(output_file):
        try:
            with open(output_file, encoding="utf-8") as f:
                saved = json.load(f)
            for d in saved:
                key = (d.get("company", ""), d.get("date", ""))
                if d.get("info"):
                    existing[key] = d["info"]
            if existing:
                print(f"Resuming: {len(existing)} deals already have info")
        except Exception:
            pass

    results = []
    for i, deal in enumerate(deals, 1):
        company = deal.get("company", "Unknown")
        key = (company, deal.get("date", ""))

        if key in existing:
            print(f"[{i}/{len(deals)}] Skipping {company} (already summarized)")
            deal["info"] = existing[key]
            results.append(deal)
            continue

        print(f"[{i}/{len(deals)}] Processing {company}...")

        source_url = deal.get("source_url", "")
        source_content = fetch_source_content(source_url)
        if source_content:
            print(f"  Fetched {len(source_content)} chars from source")
        else:
            print(f"  No source content — using fallback prompt")

        info = generate_info(
            api_key=api_key,
            company=company,
            round_type=deal.get("round", ""),
            amount_raw=deal.get("amount_raw", ""),
            source_content=source_content,
        )
        deal["info"] = info if info else "业务详情待补充"
        results.append(deal)

        # Save incrementally after each deal
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        print(f"  Info: {deal['info'][:80]}...")
        time.sleep(1.5)  # Rate limiting

    print(f"\n✓ Summarized {len(results)} deals → {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Summarize RootData deals with Claude")
    parser.add_argument("--input", required=True, help="Input JSON file (raw deals)")
    parser.add_argument("--output", required=True, help="Output JSON file (with info field)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip cost confirmation prompt")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in environment / .env")
        sys.exit(1)

    summarize_rootdata(
        input_file=args.input,
        output_file=args.output,
        api_key=api_key,
        skip_confirm=args.yes,
    )


if __name__ == "__main__":
    main()
