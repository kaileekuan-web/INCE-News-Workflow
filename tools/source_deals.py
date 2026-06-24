#!/usr/bin/env python3
"""
Deal Sourcing Agent

Uses OpenAI web search to find the top 5 early-stage companies INCE
should look at this week, based on the investment themes identified.

Input:  .tmp/debate_memos.json
Output: .tmp/deal_sourcing.json
"""

import os
import sys
import json
import argparse
import time
import requests
from dotenv import load_dotenv

load_dotenv()

OPENAI_MODEL = "gpt-4o-search-preview"

SOURCING_PROMPT = """You are a deal sourcing analyst at INCE, a VC firm focused on AI and deep tech.

Based on these investment themes identified this week:
{themes}

Search the web and identify the TOP 5 early-stage companies (Seed to Series B) that INCE should look at this week. Prioritize:
- Companies that directly address one of the themes above
- Recently announced or raised (past 2-4 weeks)
- Pre-Series B preferred (seed, pre-seed, Series A)
- Strong founding team signal

Return a JSON array — no markdown, no preamble:
[
  {{
    "company": "Company Name",
    "url": "https://company.com",
    "stage": "Seed / Series A / etc.",
    "raise": "$XM (if known, else null)",
    "thesis_fit": "1 sentence: which theme this fits and why INCE should care",
    "why_now": "1 sentence: why this week / what's the catalyst",
    "related_theme": "exact theme name from above"
  }}
]"""


def source_deals(memos: list, api_key: str) -> list:
    themes_text = "\n".join(
        f"- [{m['signal_strength']}/5] {m['theme']}: {m['insight']}"
        for m in memos
    )

    prompt = SOURCING_PROMPT.format(themes=themes_text)

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
    }

    print("  Searching for deal sourcing targets...")
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=90)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            break
        except Exception as e:
            if attempt == 2:
                print(f"  WARNING: Deal sourcing failed: {e}")
                return []
            time.sleep(10 * (attempt + 1))

    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        deals = json.loads(text)
        return deals if isinstance(deals, list) else []
    except json.JSONDecodeError:
        print("  WARNING: Could not parse deal sourcing response")
        return []


def main():
    parser = argparse.ArgumentParser(description="Source top 5 deals based on investment themes")
    parser.add_argument("--input", required=True, help="Path to debate memos JSON")
    parser.add_argument("--output", default=".tmp/deal_sourcing.json", help="Output path")
    args = parser.parse_args()

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)

    with open(args.input, encoding="utf-8") as f:
        memos = json.load(f)

    deals = source_deals(memos, openai_key)

    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)

    print(f"✓ Found {len(deals)} sourcing targets")
    for d in deals:
        print(f"  - {d.get('company')} ({d.get('stage')}) — {d.get('thesis_fit', '')[:60]}")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
