#!/usr/bin/env python3
"""
Collect TechCrunch AI articles.

Strategy (free, no paid keys required):
1. TechCrunch AI RSS feed — free, no API key, no delay. Always tried first.
2. NewsAPI.org /v2/everything — only if NEWSAPI_ORG_KEY is set. Supplements RSS
   (RSS holds ~20 recent items, NewsAPI reaches further back within the window).
   Missing key is NOT an error: we just run on RSS alone.

Results from both paths are merged and deduplicated by URL.

NewsAPI query gotcha (this cost us a silent zero-result run):
  NewsAPI treats an UNQUOTED multi-word phrase as an implicit AND of its words.
  The old query joined 23 keywords with ' OR ', several of them multi-word
  ("artificial intelligence", "large language model", ...). Those degrade into
  ANDs, so the whole expression matched nothing and the API happily returned
  status=ok with totalResults=0 — no error, no warning.
  Fix: don't send a keyword query at all. Ask for sources=techcrunch and filter
  by AI keywords locally, where we control the semantics.
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import clean_text, validate_date_range

# AI-related keywords for filtering
AI_KEYWORDS = [
    'AI', 'artificial intelligence', 'machine learning', 'ML', 'GPT',
    'LLM', 'large language model', 'ChatGPT', 'OpenAI', 'Claude',
    'neural network', 'deep learning', 'generative AI', 'computer vision',
    'NLP', 'natural language processing', 'robotics', 'autonomous',
    'Anthropic', 'Google Gemini', 'Meta AI', 'Mistral', 'AI startup'
]

# Free RSS feeds — no API key, no rate limit, no 24h delay.
RSS_FEEDS = [
    'https://techcrunch.com/category/artificial-intelligence/feed/',
    'https://techcrunch.com/feed/',  # general feed, filtered locally for AI
]


def is_ai_related(*fields: str) -> bool:
    """True if any AI keyword appears in the given text fields (word-ish match)."""
    text = ' ' + ' '.join(f or '' for f in fields).lower() + ' '
    for kw in AI_KEYWORDS:
        k = kw.lower()
        if len(k) <= 3:
            # Short tokens (AI, ML, GPT, NLP) need boundaries so "email" != "ML"
            if re.search(rf'\b{re.escape(k)}\b', text):
                return True
        elif k in text:
            return True
    return False


def collect_techcrunch_newsapi(start_date: str, end_date: str) -> list:
    """
    Collect TechCrunch articles via NewsAPI.org, filtered for AI locally.

    Returns [] (not an error) when no key is configured — RSS covers that case.
    """
    load_dotenv(override=True)
    api_key = os.getenv('NEWSAPI_ORG_KEY')

    if not api_key:
        print("  NEWSAPI_ORG_KEY not set — skipping NewsAPI (RSS only, still free)")
        return []

    try:
        from newsapi import NewsApiClient
    except ImportError:
        print("  newsapi-python not installed — skipping NewsAPI (RSS only)")
        return []

    newsapi = NewsApiClient(api_key=api_key)

    print(f"Querying NewsAPI.org for TechCrunch articles from {start_date} to {end_date}...")

    try:
        # No `q`: see the module docstring — NewsAPI's implicit-AND handling of
        # unquoted phrases silently zeroed out the old keyword query. Pull the
        # whole TechCrunch feed for the window and filter for AI here.
        response = newsapi.get_everything(
            sources='techcrunch',
            from_param=start_date,
            to=end_date,
            language='en',
            sort_by='publishedAt',
            page_size=100  # Max per request
        )

        raw = response.get('articles', [])
        print(f"  NewsAPI returned {len(raw)} TechCrunch articles in range")

        # Normalize format
        articles = []
        for article in raw:
            if not is_ai_related(article.get('title'), article.get('description')):
                continue
            articles.append({
                'source': 'TechCrunch',
                'title': clean_text(article.get('title', '')),
                'description': clean_text(article.get('description', '')),
                'url': article.get('url', ''),
                'published_at': article.get('publishedAt', ''),
                'content': clean_text(article.get('content', '')),
                'image_url': article.get('urlToImage', ''),
                'raw': article
            })

        print(f"  {len(articles)} of them are AI-related")
        return articles

    except Exception as e:
        if '429' in str(e) or 'rate limit' in str(e).lower():
            print("  WARNING: NewsAPI.org rate limit exceeded (100 requests/day) — RSS only")
        else:
            print(f"  WARNING: NewsAPI.org fetch failed ({e}) — RSS only")
        return []


def collect_techcrunch_rss(start_date: str, end_date: str) -> list:
    """
    Collect articles from TechCrunch RSS feeds. Free, no API key, no 24h delay.

    Args:
        start_date: YYYY-MM-DD format
        end_date: YYYY-MM-DD format

    Returns:
        List of normalized article dicts
    """
    try:
        import feedparser
    except ImportError:
        print("ERROR: feedparser not installed. Run: pip install feedparser")
        return []

    start_dt, end_dt = validate_date_range(start_date, end_date)
    end_dt = end_dt.replace(hour=23, minute=59, second=59)

    articles = []
    for rss_url in RSS_FEEDS:
        print(f"Fetching TechCrunch RSS: {rss_url}")
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            print(f"  WARNING: RSS fetch failed: {e}")
            continue

        found = 0
        for entry in feed.entries:
            # Parse published date
            try:
                pub_date = datetime(*entry.published_parsed[:6])
            except Exception:
                continue

            if not (start_dt <= pub_date <= end_dt):
                continue

            title = clean_text(entry.get('title', ''))
            description = clean_text(entry.get('summary', ''))
            # The general feed carries non-AI stories; the AI feed is pre-filtered.
            if 'artificial-intelligence' not in rss_url and not is_ai_related(title, description):
                continue

            articles.append({
                'source': 'TechCrunch',
                'title': title,
                'description': description,
                'url': entry.get('link', ''),
                'published_at': pub_date.isoformat() + 'Z',
                'content': clean_text(entry.get('content', [{}])[0].get('value', '') if 'content' in entry else ''),
                'raw': {'rss_feed': rss_url},
            })
            found += 1
        print(f"  {found} articles in range")

    return articles


def main():
    """Main execution function"""
    parser = argparse.ArgumentParser(description='Collect TechCrunch AI articles')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--output', default='.tmp/raw_techcrunch.json', help='Output file path')
    args = parser.parse_args()

    # Validate dates
    try:
        validate_date_range(args.start_date, args.end_date)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # RSS first (free, no key), then NewsAPI as a supplement if a key exists.
    articles = collect_techcrunch_rss(args.start_date, args.end_date)
    seen = {a['url'] for a in articles if a.get('url')}
    for a in collect_techcrunch_newsapi(args.start_date, args.end_date):
        if a.get('url') and a['url'] not in seen:
            seen.add(a['url'])
            articles.append(a)

    articles.sort(key=lambda a: a.get('published_at', ''))

    # Save to file
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Collected {len(articles)} TechCrunch articles")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
