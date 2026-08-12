#!/usr/bin/env python3
"""
Multi-Agent Debate System

For each investment insight, runs three sequential Claude calls:
  Bull Agent   → strongest case for why this matters
  Bear Agent   → strongest case for why it doesn't
  Partner Agent → final balanced take + specific action item

Input:  .tmp/insights.json
Output: .tmp/debate_memos.json (insights enriched with debate)
"""

import os
import sys
import json
import argparse
import time
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import call_claude, CLAUDE_MODEL

load_dotenv(override=True)

BULL_PROMPT = """You are the Bull analyst at INCE, a VC firm focused on AI and deep tech. Your job is to make the strongest possible investment case.

Theme: {theme}
Insight: {insight}
Rationale: {rationale}
Supporting evidence:
{articles}

Write exactly 1 paragraph (4-6 sentences) making the bull case. Why should INCE pay close attention and potentially invest? Be specific: cite the market opportunity, timing signal, and why this is defensible. Do not hedge."""

BEAR_PROMPT = """You are the Bear analyst at INCE, a VC firm. Your job is to stress-test investment theses and protect the firm from bad bets.

Theme: {theme}
Insight: {insight}
Bull case: {bull_case}

Write exactly 1 paragraph (4-6 sentences) making the bear case. What are the real risks? Is this space crowded, too early, or too late? Is the signal noise vs. signal? Push back hard on the bull case with specific reasoning."""

PARTNER_PROMPT = """You are a Senior Partner at INCE, a VC firm. You have heard the bull and bear cases. Give your final verdict.

Theme: {theme}
Insight: {insight}
Bull: {bull_case}
Bear: {bear_case}

Write 1-2 paragraphs with your final investment take. Synthesize both views honestly. End with ONE specific action item for INCE this week (e.g. "Source 3 seed-stage companies in X", "Pass — revisit in 6 months", "Add to watchlist and monitor Y metric")."""


def _call_agent(prompt: str, api_key: str = None) -> str:
    """
    One debate turn, run on Claude.

    api_key is kept in the signature only so existing call sites keep working;
    the Anthropic client reads ANTHROPIC_API_KEY from the environment. Retries
    and timeouts live in call_claude.

    Effort is 'medium': each turn is an argued position that has to engage with
    the previous one, which is the kind of work low effort thins out.
    """
    return call_claude(prompt, max_tokens=4096, effort='medium')


def debate_insight(insight: dict, api_key: str) -> dict:
    theme = insight["theme"]
    insight_text = insight["insight"]
    rationale = insight.get("rationale", "")
    articles_text = "\n".join(
        f"- {a['title']} ({a['source']})" for a in insight.get("articles", [])
    )

    print("    Bull agent...")
    bull = _call_agent(BULL_PROMPT.format(
        theme=theme, insight=insight_text,
        rationale=rationale, articles=articles_text or "(no articles cited)"
    ), api_key)
    time.sleep(0.5)

    print("    Bear agent...")
    bear = _call_agent(BEAR_PROMPT.format(
        theme=theme, insight=insight_text, bull_case=bull
    ), api_key)
    time.sleep(0.5)

    print("    Partner agent...")
    partner = _call_agent(PARTNER_PROMPT.format(
        theme=theme, insight=insight_text, bull_case=bull, bear_case=bear
    ), api_key)
    time.sleep(0.5)

    return {**insight, "debate": {"bull": bull, "bear": bear, "partner": partner}}


def main():
    parser = argparse.ArgumentParser(description="Run multi-agent debate on investment insights")
    parser.add_argument("--input", required=True, help="Path to insights JSON")
    parser.add_argument("--output", default=".tmp/debate_memos.json", help="Output path")
    args = parser.parse_args()

    api_key = None  # key is read from the environment by the Anthropic client
    print(f"Running debate on Claude ({CLAUDE_MODEL})")

    with open(args.input, encoding="utf-8") as f:
        insights = json.load(f)

    print(f"Running 3-agent debate on {len(insights)} insights...")
    memos = []
    for i, insight in enumerate(insights):
        print(f"\n  [{i+1}/{len(insights)}] {insight['theme']}")
        memo = debate_insight(insight, api_key)
        memos.append(memo)

    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(memos, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Generated {len(memos)} investment memos")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
