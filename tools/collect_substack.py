#!/usr/bin/env python3
"""
Scrape Z Potentials Substack newsletter

Strategy:
- Substack RSS feed: [URL]/feed
- Filter by date range
- Extract title, description, link, published date
- Normalize to standard format
"""

import os
import sys
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import clean_text, validate_date_range

try:
    import feedparser
except ImportError:
    print("ERROR: feedparser not installed. Run: pip install feedparser")
    sys.exit(1)


def collect_substack(url: str, start_date: str, end_date: str) -> list:
    """
    Collect Substack posts via RSS feed

    Args:
        url: Substack newsletter URL (from .env or user input)
        start_date: YYYY-MM-DD format
        end_date: YYYY-MM-DD format

    Returns:
        List of normalized article dicts
    """
    # Ensure URL ends with /feed
    if not url.endswith('/feed'):
        rss_url = f"{url.rstrip('/')}/feed"
    else:
        rss_url = url

    print(f"Fetching from Substack RSS: {rss_url}...")

    # Parse RSS feed
    feed = feedparser.parse(rss_url)

    if not feed.entries:
        print(f"WARNING: No entries found in RSS feed. Check URL: {rss_url}")
        return []

    # Validate dates
    start_dt, end_dt = validate_date_range(start_date, end_date)

    articles = []
    for entry in feed.entries:
        try:
            # Parse published date
            # Feedparser provides published_parsed which is a time struct
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                pub_date = datetime(*entry.published_parsed[:6])
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                pub_date = datetime(*entry.updated_parsed[:6])
            else:
                # Skip if no date available
                continue

            # Filter by date range
            if start_dt <= pub_date <= end_dt:
                # Extract content
                content = ''
                if hasattr(entry, 'content') and entry.content:
                    content = entry.content[0].get('value', '')
                elif hasattr(entry, 'summary'):
                    content = entry.summary

                articles.append({
                    'source': 'Z Potentials',
                    'title': clean_text(entry.get('title', '')),
                    'description': clean_text(entry.get('summary', '')),
                    'url': entry.get('link', ''),
                    'published_at': pub_date.isoformat() + 'Z',
                    'content': clean_text(content),
                    'raw': dict(entry)  # Convert to dict for JSON serialization
                })

        except Exception as e:
            print(f"WARNING: Failed to parse entry: {e}")
            continue

    return articles


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Collect Z Potentials Substack posts')
    parser.add_argument('--url', help='Substack URL (or use Z_POTENTIALS_URL in .env)')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--output', default='.tmp/raw_substack.json', help='Output file path')
    args = parser.parse_args()

    # Get URL from args or .env
    load_dotenv(override=True)
    url = args.url or os.getenv('Z_POTENTIALS_URL')

    if not url:
        print("ERROR: Must provide --url or set Z_POTENTIALS_URL in .env")
        print("Example: Z_POTENTIALS_URL=https://zpotentials.substack.com")
        sys.exit(1)

    # Validate dates
    try:
        validate_date_range(args.start_date, args.end_date)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Collect articles
    articles = collect_substack(url, args.start_date, args.end_date)

    # Save to file
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Collected {len(articles)} Substack articles")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
