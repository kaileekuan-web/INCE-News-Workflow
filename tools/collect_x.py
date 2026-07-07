#!/usr/bin/env python3
"""
Collect AI-related X/Twitter posts via Nitter RSS feeds.

X.com has no free, reliable public API and blocks scraping, so this collector
reads public Nitter RSS feeds instead. It supports two feed types:

  - Account timelines:  https://<instance>/<handle>/rss
  - Tweet searches:     https://<instance>/search/rss?f=tweets&q=<query>

Accounts and search queries are read from `x_accounts.txt` in the repo root
(see that file for format). Full tweet text is captured at collection time and
stored in `content`/`description` so the summarizer can score it WITHOUT having
to scrape x.com (which is blocked).

Reliability note: public Nitter instances are frequently rate-limited or down.
The collector tries several instances in order and moves on when one fails, so
a run may return fewer posts than expected — that is expected, not a bug.
Override the instance list with the NITTER_INSTANCES env var (comma-separated).

Output: .tmp/raw_x.json  (standard article schema, source='X/Twitter')
"""

import os
import re
import sys
import json
import argparse
from datetime import datetime
from urllib.parse import quote_plus
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import clean_text, validate_date_range

try:
    import feedparser
except ImportError:
    print("ERROR: feedparser not installed. Run: pip install feedparser")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

# Public Nitter instances tried in order. These rot constantly — override with
# the NITTER_INSTANCES env var (comma-separated, no trailing slash) when needed.
DEFAULT_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://lightbrd.com",
]

# Only keep search-feed tweets that actually mention AI (account feeds are
# already curated, so they are kept as-is).
AI_KEYWORDS = [
    'ai', 'artificial intelligence', 'machine learning', 'gpt', 'llm',
    'agent', 'model', 'openai', 'anthropic', 'claude', 'gemini', 'llama',
    'mistral', 'neural', 'inference', 'training', 'gpu', 'diffusion',
]

_STATUS_RE = re.compile(r'(?:twitter\.com|x\.com|/)([A-Za-z0-9_]+)/status/(\d+)')
_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0 Safari/537.36'),
}


def get_instances() -> list:
    override = os.getenv('NITTER_INSTANCES', '').strip()
    if override:
        return [i.strip().rstrip('/') for i in override.split(',') if i.strip()]
    return DEFAULT_INSTANCES


def load_sources(path: str) -> tuple:
    """Parse x_accounts.txt into (handles, search_queries)."""
    handles, searches = [], []
    if not os.path.exists(path):
        print(f"WARNING: {path} not found — no X sources to collect")
        return handles, searches

    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.lower().startswith('search:'):
                q = line.split(':', 1)[1].strip()
                if q:
                    searches.append(q)
            else:
                handles.append(line.lstrip('@').strip())
    return handles, searches


def _fetch_feed(path: str, instances: list):
    """Fetch an RSS path (e.g. '/OpenAI/rss') across instances until one works."""
    for base in instances:
        url = f"{base}{path}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code != 200 or not resp.content:
                continue
            feed = feedparser.parse(resp.content)
            # A working Nitter feed has entries (or at least a valid channel).
            if feed.entries or (feed.feed and feed.feed.get('title')):
                return feed, base
        except Exception:
            continue
    return None, None


def _canonical_url(entry) -> str:
    """Rewrite a Nitter status link back to a canonical x.com URL."""
    link = entry.get('link', '') or ''
    m = _STATUS_RE.search(link)
    if m:
        return f"https://x.com/{m.group(1)}/status/{m.group(2)}"
    return link


def _entry_datetime(entry):
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except Exception:
        return None


def _is_ai_relevant(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in AI_KEYWORDS)


def _normalize_entry(entry, source_label: str, handle_hint: str = '') -> dict:
    text = clean_text(entry.get('title', ''))
    url = _canonical_url(entry)
    # Author handle: prefer the URL, fall back to the feed author or hint.
    m = _STATUS_RE.search(url)
    handle = m.group(1) if m else (entry.get('author', '') or handle_hint).lstrip('@')
    dt = _entry_datetime(entry)

    title = f"@{handle}: {text}" if handle else text
    if len(title) > 140:
        title = title[:137] + '...'

    return {
        'source': source_label,
        'title': clean_text(title),
        'description': text,
        'url': url,
        'published_at': (dt.isoformat() + 'Z') if dt else '',
        'content': text,          # full tweet text — lets summarizer score without scraping x.com
        'author': f"@{handle}" if handle else '',
        'raw': {'nitter_link': entry.get('link', '')},
    }


def collect_x(start_date: str, end_date: str, accounts_file: str) -> list:
    start_dt, end_dt = validate_date_range(start_date, end_date)
    # Include the whole end day.
    end_dt = end_dt.replace(hour=23, minute=59, second=59)

    instances = get_instances()
    handles, searches = load_sources(accounts_file)
    print(f"X/Twitter sources: {len(handles)} accounts, {len(searches)} searches")
    print(f"Nitter instances: {', '.join(instances)}")

    articles = []
    seen_urls = set()
    working_instance = None  # reuse the first instance that works

    def add_entries(feed, source_label, keyword_filter, handle_hint=''):
        added = 0
        for entry in feed.entries:
            title = entry.get('title', '') or ''
            # Skip retweets — Nitter prefixes them "RT by @user:".
            if title.startswith('RT by '):
                continue
            dt = _entry_datetime(entry)
            if dt is None or not (start_dt <= dt <= end_dt):
                continue
            if keyword_filter and not _is_ai_relevant(title):
                continue
            art = _normalize_entry(entry, source_label, handle_hint)
            if not art['url'] or art['url'] in seen_urls:
                continue
            seen_urls.add(art['url'])
            articles.append(art)
            added += 1
        return added

    # Prefer a known-good instance once we find one, but keep the full list as fallback.
    def ordered_instances():
        if working_instance:
            return [working_instance] + [i for i in instances if i != working_instance]
        return instances

    # 1) Account timelines
    for handle in handles:
        feed, base = _fetch_feed(f"/{handle}/rss", ordered_instances())
        if feed is None:
            print(f"  [skip] @{handle}: no Nitter instance responded")
            continue
        working_instance = base
        n = add_entries(feed, 'X/Twitter', keyword_filter=False, handle_hint=handle)
        print(f"  @{handle}: {n} posts in range")

    # 2) Tweet searches (best-effort — search is often disabled)
    for query in searches:
        path = f"/search/rss?f=tweets&q={quote_plus(query)}"
        feed, base = _fetch_feed(path, ordered_instances())
        if feed is None:
            print(f"  [skip] search '{query}': no instance served results (search may be disabled)")
            continue
        working_instance = base
        n = add_entries(feed, 'X/Twitter (search)', keyword_filter=True)
        print(f"  search '{query}': {n} AI posts in range")

    return articles


def main():
    parser = argparse.ArgumentParser(description='Collect AI X/Twitter posts via Nitter RSS')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--accounts', default='x_accounts.txt',
                        help='Path to accounts/searches file (default: x_accounts.txt)')
    parser.add_argument('--output', default='.tmp/raw_x.json', help='Output file path')
    args = parser.parse_args()

    load_dotenv()

    try:
        validate_date_range(args.start_date, args.end_date)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    articles = collect_x(args.start_date, args.end_date, args.accounts)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Collected {len(articles)} X/Twitter posts")
    if not articles:
        print("  (0 posts — Nitter instances may be down/rate-limited. "
              "Try again later or set NITTER_INSTANCES in .env)")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
