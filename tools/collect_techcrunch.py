#!/usr/bin/env python3
"""
Collect TechCrunch AI articles using NewsAPI.org

Strategy:
- Use NewsAPI.org /v2/everything endpoint
- Filter: source=techcrunch, AI keywords in title/description
- Date range: user-provided start/end dates
- Output: Normalized JSON format
- Fallback: RSS feed if rate limit exceeded
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

# AI-related keywords for filtering
AI_KEYWORDS = [
    'AI', 'artificial intelligence', 'machine learning', 'ML', 'GPT',
    'LLM', 'large language model', 'ChatGPT', 'OpenAI', 'Claude',
    'neural network', 'deep learning', 'generative AI', 'computer vision',
    'NLP', 'natural language processing', 'robotics', 'autonomous',
    'Anthropic', 'Google Gemini', 'Meta AI', 'Mistral', 'AI startup'
]


def collect_techcrunch_newsapi(start_date: str, end_date: str) -> list:
    """
    Collect articles from TechCrunch via NewsAPI.org

    Args:
        start_date: YYYY-MM-DD format
        end_date: YYYY-MM-DD format

    Returns:
        List of normalized article dicts
    """
    try:
        from newsapi import NewsApiClient
    except ImportError:
        print("ERROR: newsapi-python not installed. Run: pip install newsapi-python")
        sys.exit(1)

    load_dotenv(override=True)
    api_key = os.getenv('NEWSAPI_ORG_KEY')

    if not api_key:
        print("ERROR: NEWSAPI_ORG_KEY not found in .env file")
        print("Get a free key at: https://newsapi.org/")
        sys.exit(1)

    newsapi = NewsApiClient(api_key=api_key)

    # Build query with AI keywords
    query = ' OR '.join(AI_KEYWORDS)

    print(f"Querying NewsAPI.org for TechCrunch articles from {start_date} to {end_date}...")

    try:
        # Fetch articles
        response = newsapi.get_everything(
            q=query,
            sources='techcrunch',
            from_param=start_date,
            to=end_date,
            language='en',
            sort_by='publishedAt',
            page_size=100  # Max per request
        )

        # Normalize format
        articles = []
        for article in response.get('articles', []):
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

        return articles

    except Exception as e:
        if '429' in str(e) or 'rate limit' in str(e).lower():
            print("WARNING: NewsAPI.org rate limit exceeded (100 requests/day)")
            print("Falling back to RSS feed...")
            return collect_techcrunch_rss(start_date, end_date)
        else:
            print(f"ERROR: Failed to fetch from NewsAPI.org: {e}")
            return []


def collect_techcrunch_rss(start_date: str, end_date: str) -> list:
    """
    Fallback: Collect articles from TechCrunch RSS feed

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

    print("Fetching from TechCrunch AI RSS feed...")

    rss_url = 'https://techcrunch.com/category/artificial-intelligence/feed/'
    feed = feedparser.parse(rss_url)

    start_dt, end_dt = validate_date_range(start_date, end_date)

    articles = []
    for entry in feed.entries:
        # Parse published date
        try:
            pub_date = datetime(*entry.published_parsed[:6])
        except:
            continue

        # Filter by date range
        if start_dt <= pub_date <= end_dt:
            articles.append({
                'source': 'TechCrunch',
                'title': clean_text(entry.get('title', '')),
                'description': clean_text(entry.get('summary', '')),
                'url': entry.get('link', ''),
                'published_at': pub_date.isoformat() + 'Z',
                'content': clean_text(entry.get('content', [{}])[0].get('value', '') if 'content' in entry else ''),
                'raw': entry
            })

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

    # Collect articles
    articles = collect_techcrunch_newsapi(args.start_date, args.end_date)

    # Save to file
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Collected {len(articles)} TechCrunch articles")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
