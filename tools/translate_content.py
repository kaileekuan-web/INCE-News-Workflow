#!/usr/bin/env python3
"""
Translate article descriptions to Simplified Chinese with Claude.

Standalone utility — no pipeline calls this (see HANDOFF.md). The report
pipelines summarize straight into Chinese via summarize_articles.py --language
zh, which is one call per article instead of summarize-then-translate. Kept for
one-off backfills: translating a batch of articles that were collected in
English after the fact.

Translation itself is delegated to translate_to_chinese_claude() in
tools/utils.py — the same function the rest of the pipeline uses, so its
proper-noun rules (ByteDance must not come back as "BiteDance") apply here too
rather than being reimplemented with a second prompt that would drift.

Input: .tmp/classified_articles.json
Output: .tmp/translated_articles.json (with chinese_description field added)
"""

import os
import sys
import json
import argparse
from typing import List

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import translate_to_chinese_claude


def estimate_cost(descriptions: List[str]) -> float:
    """
    Rough cost estimate for the translation run, used only to gate the
    confirmation prompt below.

    Assumes Opus 5 list rates ($5 / $25 per MTok) and ~4 characters per token,
    with the Chinese output about the same token count as the English input
    plus low-effort thinking headroom. Over-estimates on Sonnet or Haiku, and
    it is an estimate — the real invoice is whatever the API bills.
    """
    total_chars = sum(len(d) for d in descriptions)
    input_tokens = total_chars // 4
    output_tokens = input_tokens

    return (input_tokens / 1_000_000) * 5.0 + (output_tokens / 1_000_000) * 25.0


def translate_batch(texts: List[str]) -> List[str]:
    """
    Translate texts to Chinese, one call per text.

    Deliberately not batched. The previous implementation packed 20 items into
    one numbered prompt and split the reply on newlines — when the model
    returned a different number of lines than it was given (a translation
    wrapping onto two lines was enough), every translation after that point
    silently shifted onto the wrong article. One call per text costs more but
    cannot misalign, and this is an occasional backfill tool, not a hot path.

    Returns a list the same length as `texts`; a failed or empty translation
    is '' so the caller can tell it apart from a real result.
    """
    translations = []

    for i, text in enumerate(texts, 1):
        if not text:
            translations.append('')
            continue

        print(f"Translating {i}/{len(texts)}...")
        try:
            translations.append(translate_to_chinese_claude(text))
        except Exception as e:
            # call_claude already swallows API-level failures and returns '';
            # this catches anything unexpected so one bad article can't abort
            # a long backfill after most of it has succeeded.
            print(f"  WARNING: Translation failed for item {i}: {e}")
            translations.append('')

    return translations


def translate_articles(input_file: str = '.tmp/classified_articles.json',
                       output_file: str = '.tmp/translated_articles.json'):
    """
    Main translation function

    Args:
        input_file: Path to classified articles JSON
        output_file: Path to output translated articles JSON
    """
    load_dotenv(override=True)

    if not os.getenv('ANTHROPIC_API_KEY'):
        print("ERROR: ANTHROPIC_API_KEY not found in .env file")
        print("Get your API key at: https://console.anthropic.com/settings/keys")
        sys.exit(1)

    # Load classified articles
    print(f"Loading articles from {input_file}...")
    with open(input_file, 'r', encoding='utf-8') as f:
        articles = json.load(f)

    print(f"Loaded {len(articles)} articles")

    # Extract descriptions for translation
    descriptions = [article.get('description', '') or article.get('title', '') for article in articles]

    # Filter out empty descriptions
    non_empty_count = sum(1 for d in descriptions if d)
    print(f"Articles with content to translate: {non_empty_count}")

    # Estimate cost
    estimated_cost = estimate_cost(descriptions)
    print(f"\nEstimated cost: ${estimated_cost:.3f}")

    # Ask for confirmation if cost is high
    if estimated_cost > 1.0:
        response = input(f"\nTranslation will cost approximately ${estimated_cost:.2f}. Continue? (y/n): ")
        if response.lower() != 'y':
            print("Translation cancelled")
            sys.exit(0)

    # Translate
    print("\nStarting translation...")
    translations = translate_batch(descriptions)

    failed = sum(1 for t, d in zip(translations, descriptions) if d and not t)
    print(f"\n✓ Translation complete ({len(translations)} items)")
    if failed:
        print(f"  WARNING: {failed} item(s) came back empty and were left untranslated")

    # Add translations to articles
    for article, translation in zip(articles, translations):
        article['chinese_description'] = translation

    # Save output
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"✓ Saved to {output_file}")


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Translate article descriptions to Chinese')
    parser.add_argument('--input', default='.tmp/classified_articles.json', help='Input file')
    parser.add_argument('--output', default='.tmp/translated_articles.json', help='Output file')
    args = parser.parse_args()

    translate_articles(args.input, args.output)


if __name__ == "__main__":
    main()
