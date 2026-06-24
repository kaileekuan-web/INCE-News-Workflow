#!/usr/bin/env python3
"""
Signal → Insight Agent

Reads summarized articles and uses Claude to:
1. Cluster articles into 3-7 investment themes
2. Generate a one-liner insight per theme (e.g. "3 startups raised in AI agent security → emerging infra gap")
3. Score each theme by investment signal strength (1-5)

Input:  .tmp/summarized_articles.json
Output: .tmp/insights.json
"""

import os
import sys
import json
import argparse
import requests
from dotenv import load_dotenv

load_dotenv()

CLAUDE_MODEL = "claude-sonnet-4-6"

INSIGHT_PROMPT = """You are a senior VC analyst at INCE, a venture capital firm focused on AI and emerging technology.

You have been given {n} AI/tech news articles from the past two weeks. Your job is to:
1. Identify 3-7 distinct investment themes emerging from these articles
2. For each theme, write a one-liner insight like: "3 startups raised in AI agent security this week → emerging infra gap"
3. Score the investment signal strength 1-5 (5 = strong actionable signal for INCE to act on now)
4. Write 2-3 sentences of VC rationale explaining why this matters for early-stage investing

Return a JSON array — no markdown, no preamble, just the array:
[
  {{
    "theme": "short theme name (4-7 words)",
    "insight": "one-liner insight with → arrow",
    "signal_strength": <1-5>,
    "rationale": "2-3 sentences of VC rationale",
    "article_indices": [<list of 0-based indices of supporting articles>],
    "companies_mentioned": ["Company A", "Company B"]
  }}
]

Articles (index: title | source | summary):
{articles}"""


def generate_insights(articles: list, api_key: str) -> list:
    formatted = []
    for i, a in enumerate(articles):
        title = a.get("title", "")
        source = a.get("source", "")
        summary = a.get("summary", a.get("description", ""))[:400]
        formatted.append(f"{i}: {title} | {source} | {summary}")

    prompt = INSIGHT_PROMPT.format(n=len(articles), articles="\n".join(formatted))

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }

    print("  Clustering articles into investment themes...")
    response = requests.post(url, json=payload, headers=headers, timeout=90)
    response.raise_for_status()

    text = response.json()["content"][0]["text"].strip()
    # Strip markdown code fences if present
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    insights = json.loads(text)

    # Attach actual article objects to each insight
    for insight in insights:
        cited = []
        for idx in insight.get("article_indices", []):
            if 0 <= idx < len(articles):
                a = articles[idx]
                cited.append({
                    "title": a.get("title", ""),
                    "url": a.get("url", ""),
                    "source": a.get("source", ""),
                    "published_at": a.get("published_at", ""),
                })
        insight["articles"] = cited

    insights.sort(key=lambda x: x.get("signal_strength", 0), reverse=True)
    return insights


def main():
    parser = argparse.ArgumentParser(description="Generate investment insights from summarized articles")
    parser.add_argument("--input", required=True, help="Path to summarized articles JSON")
    parser.add_argument("--output", default=".tmp/insights.json", help="Output path")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    with open(args.input, encoding="utf-8") as f:
        articles = json.load(f)

    if not articles:
        print("ERROR: No articles found in input file")
        sys.exit(1)

    print(f"Analyzing {len(articles)} articles...")
    insights = generate_insights(articles, api_key)

    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(insights, f, ensure_ascii=False, indent=2)

    print(f"✓ Generated {len(insights)} investment insights")
    for ins in insights:
        print(f"  [{ins['signal_strength']}/5] {ins['insight']}")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
