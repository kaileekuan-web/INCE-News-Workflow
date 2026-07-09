#!/usr/bin/env python3
"""
Translate article descriptions to Chinese using OpenAI GPT

Strategy:
- Batch translate to minimize API calls
- Use GPT-4o-mini for cost efficiency
- Low temperature for consistency
- Estimate cost before running

Input: .tmp/classified_articles.json
Output: .tmp/translated_articles.json (with chinese_description field added)
"""

import os
import sys
import json
import argparse
from typing import List
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package not installed. Run: pip install openai")
    sys.exit(1)


def estimate_cost(descriptions: List[str]) -> float:
    """
    Estimate translation cost

    Args:
        descriptions: List of English descriptions

    Returns:
        Estimated cost in USD
    """
    # Rough estimate: 4 characters per token
    total_chars = sum(len(d) for d in descriptions)
    estimated_input_tokens = total_chars // 4

    # Output is roughly same length in Chinese
    estimated_output_tokens = estimated_input_tokens

    # GPT-4o-mini pricing (as of 2026)
    # Input: $0.15 / 1M tokens
    # Output: $0.60 / 1M tokens
    input_cost = (estimated_input_tokens / 1_000_000) * 0.15
    output_cost = (estimated_output_tokens / 1_000_000) * 0.60

    return input_cost + output_cost


def translate_batch(client: OpenAI, texts: List[str], batch_size: int = 20) -> List[str]:
    """
    Translate a batch of texts

    Args:
        client: OpenAI client
        texts: List of English texts
        batch_size: Number of texts per API call

    Returns:
        List of Chinese translations
    """
    translations = []

    total_batches = (len(texts) + batch_size - 1) // batch_size

    for batch_num, i in enumerate(range(0, len(texts), batch_size), 1):
        batch = texts[i:i+batch_size]

        print(f"Translating batch {batch_num}/{total_batches} ({len(batch)} items)...")

        # Format prompt with numbered items
        prompt = "Translate the following English news descriptions to Chinese (Simplified). Return only the translations, numbered:\n\n"
        for idx, text in enumerate(batch, 1):
            prompt += f"{idx}. {text}\n\n"

        try:
            # Call GPT
            response = client.chat.completions.create(
                model="gpt-4o-mini",  # Cheaper model sufficient for translation
                messages=[
                    {
                        "role": "system",
                        "content": "You are a professional translator specializing in technology news. Translate accurately and naturally to Simplified Chinese. Preserve technical terms in English where appropriate."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.3  # Low temp for consistency
            )

            # Parse response
            translated_text = response.choices[0].message.content.strip()

            # Split by lines and remove numbering
            translated_batch = []
            for line in translated_text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                # Remove numbering (e.g., "1. " or "1) ")
                line = re.sub(r'^\d+[\.\)]\s*', '', line)
                if line:
                    translated_batch.append(line)

            # Handle case where GPT didn't number the output
            if len(translated_batch) != len(batch):
                print(f"WARNING: Expected {len(batch)} translations, got {len(translated_batch)}")
                # Pad with empty strings if needed
                while len(translated_batch) < len(batch):
                    translated_batch.append("")

            translations.extend(translated_batch[:len(batch)])

        except Exception as e:
            print(f"ERROR: Translation failed for batch {batch_num}: {e}")
            # Add empty translations for failed batch
            translations.extend([''] * len(batch))

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

    # Check for API key
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        print("ERROR: OPENAI_API_KEY not found in .env file")
        print("Get your API key at: https://platform.openai.com/api-keys")
        sys.exit(1)

    # Initialize OpenAI client
    client = OpenAI(api_key=api_key)

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
    translations = translate_batch(client, descriptions)

    print(f"\n✓ Translation complete ({len(translations)} items)")

    # Add translations to articles
    for article, translation in zip(articles, translations):
        article['chinese_description'] = translation

    # Save output
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"✓ Saved to {output_file}")


# Import re for regex operations
import re


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Translate article descriptions to Chinese')
    parser.add_argument('--input', default='.tmp/classified_articles.json', help='Input file')
    parser.add_argument('--output', default='.tmp/translated_articles.json', help='Output file')
    args = parser.parse_args()

    translate_articles(args.input, args.output)


if __name__ == "__main__":
    main()
