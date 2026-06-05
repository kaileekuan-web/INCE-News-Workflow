#!/usr/bin/env python3
"""
Deduplicate collected articles by exact URL and title similarity.

Replaces the inline Phase 3 deduplication in the workflow.
Reads raw source files, removes duplicates, writes classified_articles.json.

Usage:
    python tools/dedup_articles.py
    python tools/dedup_articles.py --threshold 0.5
"""

import json
import re
import argparse
import os

STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'has', 'have', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'this', 'that', 'these', 'those', 'it', 'its',
    'as', 'up', 'out', 'about', 'into', 'through', 'new', 'can', 'how',
    'what', 'says', 'said', 'after', 'over', 'now', 'just', 'also',
}


def title_words(title: str) -> set:
    """Normalize a title into a set of significant words."""
    title = title.lower()
    title = re.sub(r'[^\w\s]', ' ', title)
    return {w for w in title.split() if w not in STOP_WORDS and len(w) > 2}


def jaccard(set1: set, set2: set) -> float:
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)


def dedup_articles(articles: list, threshold: float = 0.5) -> tuple:
    """
    Two-pass deduplication:
      Pass 1 — exact URL match
      Pass 2 — title word overlap (Jaccard >= threshold)

    Returns (unique_articles, url_dupes_removed, title_dupes_removed).
    """
    # Pass 1: exact URL
    seen_urls: set = set()
    url_deduped = []
    url_dupes = 0
    for a in articles:
        url = a.get('url', '').strip()
        if url and url in seen_urls:
            url_dupes += 1
            continue
        if url:
            seen_urls.add(url)
        url_deduped.append(a)

    # Pass 2: title similarity
    unique = []
    unique_word_sets = []
    title_dupes = 0
    for a in url_deduped:
        words = title_words(a.get('title', ''))
        is_dupe = any(jaccard(words, existing) >= threshold for existing in unique_word_sets)
        if is_dupe:
            title_dupes += 1
        else:
            unique.append(a)
            unique_word_sets.append(words)

    return unique, url_dupes, title_dupes


def main():
    parser = argparse.ArgumentParser(description='Deduplicate collected articles')
    parser.add_argument('--threshold', type=float, default=0.5,
                        help='Jaccard similarity threshold for title dedup (default: 0.5)')
    parser.add_argument('--tldr-ai', default='.tmp/raw_tldr_ai.json')
    parser.add_argument('--tldr-main', default='.tmp/raw_tldr_main.json')
    parser.add_argument('--techcrunch', default='.tmp/raw_techcrunch.json')
    parser.add_argument('--output', default='.tmp/classified_articles.json')
    args = parser.parse_args()

    all_articles = []
    for path, label in [
        (args.tldr_ai, 'TLDR AI'),
        (args.tldr_main, 'TLDR Main'),
        (args.techcrunch, 'TechCrunch'),
    ]:
        if os.path.exists(path):
            with open(path) as f:
                batch = json.load(f)
            all_articles.extend(batch)
            print(f"  Loaded {len(batch):>4} articles from {label}")
        else:
            print(f"  WARNING: {path} not found, skipping")

    print(f"  Total before dedup: {len(all_articles)}")

    unique, url_dupes, title_dupes = dedup_articles(all_articles, args.threshold)

    print(f"  Removed {url_dupes} exact URL duplicates")
    print(f"  Removed {title_dupes} similar-title duplicates (threshold={args.threshold})")
    print(f"  Unique articles: {len(unique)}")

    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)

    print(f"  Saved to {args.output}")


if __name__ == '__main__':
    main()
