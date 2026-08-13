#!/usr/bin/env python3
"""
Deduplicate collected articles before summarization.

Every duplicate that survives this step costs an LLM call and then shows up
twice in the report, so this runs on the raw collector output — one merged,
deduplicated file for the summarizer to work through.

The comparison itself lives in tools/news_filters.dedupe, which is also called
after source resolution and again at document generation. Four ways the same
news arrives twice, all handled: the same link (any spelling of it), the same
wording, two wordings that still overlap heavily, and two write-ups that share
the story's names and figures without overlapping much at all. The richest copy
survives — see news_filters._richness.

Usage:
    python tools/dedup_articles.py                      # every raw_*.json in .tmp/
    python tools/dedup_articles.py --inputs a.json b.json
    python tools/dedup_articles.py --threshold 0.5      # looser near-text match
"""

import json
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.news_filters import dedupe

# Collector outputs, in the order they should be merged. Missing files are
# skipped: which collectors ran depends on the workflow.
DEFAULT_INPUTS = [
    ('.tmp/raw_x.json', 'X/Twitter'),
    ('.tmp/raw_tldr_ai.json', 'TLDR AI'),
    ('.tmp/raw_tldr_main.json', 'TLDR Main'),
    ('.tmp/raw_techcrunch.json', 'TechCrunch'),
    ('.tmp/raw_wechat.json', 'WeChat'),
]


def main():
    parser = argparse.ArgumentParser(description='Deduplicate collected articles')
    parser.add_argument('--inputs', nargs='+', metavar='FILE',
                        help='Article JSON files to merge (default: every known '
                             'raw_*.json in .tmp/ that exists)')
    parser.add_argument('--threshold', type=float, default=0.55,
                        help='Word-overlap threshold for near-duplicate detection '
                             '(default: 0.55; lower is more aggressive)')
    parser.add_argument('--output', default='.tmp/classified_articles.json')
    args = parser.parse_args()

    sources = ([(p, os.path.basename(p)) for p in args.inputs] if args.inputs
               else DEFAULT_INPUTS)

    all_articles = []
    for path, label in sources:
        if not os.path.exists(path):
            if args.inputs:
                print(f"  WARNING: {path} not found, skipping")
            continue
        with open(path, encoding='utf-8') as f:
            batch = json.load(f)
        all_articles.extend(batch)
        print(f"  Loaded {len(batch):>4} articles from {label}")

    if not all_articles:
        print("ERROR: no input articles found — did a collector run?")
        sys.exit(1)

    print(f"  Total before dedup: {len(all_articles)}")

    unique, stats = dedupe(all_articles, threshold=args.threshold)

    for key, label in (('same-url', 'same link'),
                       ('same-text', 'identical wording'),
                       ('near-text', 'same story, different wording'),
                       ('same-story', 'same names and figures')):
        if stats[key]:
            print(f"  Removed {stats[key]:>3} duplicate(s): {label}")
    print(f"  Unique articles: {len(unique)}")

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)

    print(f"  Saved to {args.output}")


if __name__ == '__main__':
    main()
