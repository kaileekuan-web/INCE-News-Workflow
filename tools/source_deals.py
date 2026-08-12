#!/usr/bin/env python3
"""
Deal Sourcing Agent

Uses Claude's server-side web search to find the top 5 early-stage companies
INCE should look at this week, based on the investment themes identified.

Input:  .tmp/debate_memos.json
Output: .tmp/deal_sourcing.json
"""

import os
import sys
import json
import argparse

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import call_claude_search

load_dotenv(override=True)

SOURCING_PROMPT = """You are a deal sourcing analyst at INCE, a VC firm focused on AI and deep tech.

Based on these investment themes identified this week:
{themes}

Search the web and identify the TOP 5 early-stage companies (Seed to Series B) that INCE should look at this week. Prioritize:
- Companies that directly address one of the themes above
- Recently announced or raised (past 2-4 weeks)
- Pre-Series B preferred (seed, pre-seed, Series A)
- Strong founding team signal

For each company report:
- company: company name
- url: company website or the most relevant source link
- stage: Seed / Series A / etc. ("unknown" if not established)
- raise: amount raised ("unknown" if not established)
- thesis_fit: 1 sentence — which theme this fits and why INCE should care
- why_now: 1 sentence — why this week / what's the catalyst
- related_theme: exact theme name from the list above

Only include companies you found real sources for. Fewer than 5 is correct if that is all you can verify."""


DEALS_SCHEMA = {
    "type": "object",
    "properties": {
        "deals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "url": {"type": "string"},
                    "stage": {"type": "string"},
                    "raise": {"type": "string"},
                    "thesis_fit": {"type": "string"},
                    "why_now": {"type": "string"},
                    "related_theme": {"type": "string"},
                },
                "required": ["company", "url", "stage", "raise", "thesis_fit",
                             "why_now", "related_theme"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["deals"],
    "additionalProperties": False,
}


def source_deals(memos: list, api_key: str = None) -> list:
    """Find sourcing targets with Claude's server-side web search.

    api_key is accepted for call-site compatibility and ignored — the Anthropic
    client reads ANTHROPIC_API_KEY from the environment.
    """
    themes_text = "\n".join(
        f"- [{m['signal_strength']}/5] {m['theme']}: {m['insight']}"
        for m in memos
    )

    prompt = SOURCING_PROMPT.format(themes=themes_text)

    print("  Searching for deal sourcing targets...")
    result = call_claude_search(prompt, schema=DEALS_SCHEMA, max_uses=12,
                                label="deal sourcing")
    if not result:
        return []

    deals = result.get("deals")
    return deals if isinstance(deals, list) else []


def main():
    parser = argparse.ArgumentParser(description="Source top 5 deals based on investment themes")
    parser.add_argument("--input", required=True, help="Path to debate memos JSON")
    parser.add_argument("--output", default=".tmp/deal_sourcing.json", help="Output path")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    with open(args.input, encoding="utf-8") as f:
        memos = json.load(f)

    deals = source_deals(memos)

    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)

    print(f"✓ Found {len(deals)} sourcing targets")
    for d in deals:
        print(f"  - {d.get('company')} ({d.get('stage')}) — {d.get('thesis_fit', '')[:60]}")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
