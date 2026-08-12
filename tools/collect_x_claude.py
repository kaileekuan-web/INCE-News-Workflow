#!/usr/bin/env python3
"""
Claude-powered fallback for X/Twitter collection.

`collect_x.py` reads public Nitter RSS mirrors, which are the fast, high-fidelity
path when they work — and they rot without warning. This module is the safety
net: it asks Claude, with its server-side web search tool, what the accounts we
follow actually announced in the window, and returns the same article dicts the
Nitter path produces.

**What this returns is not the tweet itself.** x.com blocks automated readers, so
Claude finds the announcement wherever it was reported — the post if a source
quotes it, otherwise the coverage of it. Items are therefore labelled
`X/Twitter (via Claude search)` so provenance stays visible all the way into the
Word document, and the URL points at whatever source actually backs the claim.

Two entry points, both used by collect_x.py and both runnable standalone:

  collect_accounts_via_claude()  — what did these handles announce?
  collect_query_via_claude()     — the `search:` lines in x_accounts.txt, which
                                   public Nitter instances stopped serving

Output (standalone): .tmp/raw_x_claude.json
"""

import os
import sys
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import clean_text, call_claude_search, validate_date_range

SOURCE_LABEL = 'X/Twitter (via Claude search)'

# Accounts are searched in small batches. One handle per call wastes a search
# turn on quiet accounts; the whole list at once makes the model spread a fixed
# search budget too thin and skip accounts silently.
DEFAULT_BATCH_SIZE = 6

# Schema for the reply. Constrained decoding means the shape is guaranteed, so
# there is no regex-scraping of prose to break when the model's phrasing shifts.
# `additionalProperties: false` + every field required is what the API needs to
# compile a strict schema.
POSTS_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "X handle without the @, exactly as given in the request",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date of the post/announcement, YYYY-MM-DD",
                    },
                    "text": {
                        "type": "string",
                        "description": "What was announced, 1-3 factual sentences",
                    },
                    "url": {
                        "type": "string",
                        "description": "Direct source URL backing this item",
                    },
                    "is_direct_post": {
                        "type": "boolean",
                        "description": "true if url is the x.com post itself, false if it is coverage of it",
                    },
                },
                "required": ["handle", "date", "text", "url", "is_direct_post"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}


def _accounts_prompt(handles: list, start_date: str, end_date: str, limit: int) -> str:
    handle_list = ', '.join(f"@{h}" for h in handles)
    return f"""Search the web for AI news that these X/Twitter accounts posted or announced between {start_date} and {end_date} (inclusive): {handle_list}

These are AI labs, founders, researchers and investors. I want what they actually announced or reported in that window — model releases, product launches, funding, research results, notable company news.

Rules:
- Only include items you found a real source for. Do not infer, summarize from memory, or fill gaps.
- Only include items dated within {start_date} to {end_date}. If you cannot establish the date from a source, leave the item out.
- One entry per distinct announcement. Do not report the same news twice under two handles.
- Skip pure commentary, jokes, personal posts, and promotional calls to action.
- `text` states what happened, in 1-3 sentences, using only facts present in the source.
- `url` is the most direct source: the x.com post itself when a source gives it (set is_direct_post true), otherwise the article reporting it (false).
- Return at most {limit} items. Fewer is correct if that is all you can verify — an empty list is a valid answer."""


def _query_prompt(query: str, start_date: str, end_date: str, limit: int) -> str:
    return f"""Search the web for AI news matching this topic between {start_date} and {end_date} (inclusive): "{query}"

Rules:
- Only include items you found a real source for. Do not infer or fill gaps from memory.
- Only include items dated within {start_date} to {end_date}. If you cannot establish the date from a source, leave the item out.
- One entry per distinct event; no duplicates of the same news.
- `handle` is the X handle of the company or person the news is about, without the @ (best effort — use the company's own account when you know it, otherwise an empty string).
- `text` states what happened, in 1-3 sentences, using only facts present in the source.
- `url` is the source article URL (set is_direct_post false unless it is literally an x.com post).
- Return at most {limit} items. An empty list is a valid answer."""


def _to_article(post: dict, start_dt: datetime, end_dt: datetime) -> dict:
    """
    Convert one schema-validated item into the pipeline's article dict.

    Returns None when the item fails validation. The date check is the important
    one: a web search reaches for near-misses outside the window when the exact
    range is quiet, and those would silently widen the report's date range.
    """
    url = (post.get('url') or '').strip()
    text = clean_text(post.get('text') or '')
    if not url or not url.startswith('http') or len(text) < 40:
        return None

    date_str = (post.get('date') or '').strip()[:10]
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        return None
    if not (start_dt <= dt <= end_dt):
        return None

    handle = (post.get('handle') or '').lstrip('@').strip()
    title = f"@{handle}: {text}" if handle else text
    if len(title) > 140:
        title = title[:137] + '...'

    return {
        'source': SOURCE_LABEL,
        'title': clean_text(title),
        'description': text,
        'url': url,
        'published_at': dt.isoformat() + 'Z',
        'content': text,
        'author': f"@{handle}" if handle else '',
        # Downstream (dedup, summarizer, Word doc) treats this like any other
        # article; the flag is here so a reader of the JSON can tell how the
        # item was obtained without inferring it from the source label.
        'via_claude_search': True,
        'raw': {'is_direct_post': bool(post.get('is_direct_post'))},
    }


def _run_search(prompt: str, start_dt: datetime, end_dt: datetime, label: str) -> list:
    result = call_claude_search(prompt, schema=POSTS_SCHEMA, max_uses=10, label=label)
    if not result:
        return []

    posts = result.get('posts') if isinstance(result, dict) else None
    if not isinstance(posts, list):
        return []

    articles = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        article = _to_article(post, start_dt, end_dt)
        if article:
            articles.append(article)
    return articles


def collect_accounts_via_claude(handles: list, start_date: str, end_date: str,
                                batch_size: int = DEFAULT_BATCH_SIZE,
                                per_batch_limit: int = 8) -> list:
    """
    Ask Claude what `handles` announced in the window. Returns article dicts.

    Handles are searched in batches of `batch_size` so one call covers several
    accounts without the model rationing its searches across too many at once.
    """
    handles = [h.lstrip('@').strip() for h in handles if h and h.strip()]
    if not handles:
        return []

    start_dt, end_dt = validate_date_range(start_date, end_date)
    end_dt = end_dt.replace(hour=23, minute=59, second=59)

    articles = []
    batches = [handles[i:i + batch_size] for i in range(0, len(handles), batch_size)]
    for i, batch in enumerate(batches, 1):
        names = ', '.join('@' + h for h in batch)
        print(f"  [claude {i}/{len(batches)}] searching {names}...")
        found = _run_search(
            _accounts_prompt(batch, start_date, end_date, per_batch_limit),
            start_dt, end_dt, label=f"X account search ({names})",
        )
        print(f"    found {len(found)} item(s)")
        articles.extend(found)

    return articles


def collect_query_via_claude(query: str, start_date: str, end_date: str,
                             limit: int = 8) -> list:
    """Run one `search:` query from x_accounts.txt through Claude web search."""
    start_dt, end_dt = validate_date_range(start_date, end_date)
    end_dt = end_dt.replace(hour=23, minute=59, second=59)

    return _run_search(
        _query_prompt(query, start_date, end_date, limit),
        start_dt, end_dt, label=f"X topic search '{query}'",
    )


def main():
    parser = argparse.ArgumentParser(
        description='Collect AI news for X/Twitter accounts via Claude web search '
                    '(fallback for when Nitter is down)')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--accounts', default='x_accounts.txt',
                        help='Path to accounts/searches file (default: x_accounts.txt)')
    parser.add_argument('--handles', default=None,
                        help='Comma-separated handles to search instead of reading --accounts')
    parser.add_argument('--output', default='.tmp/raw_x_claude.json', help='Output file path')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
                        help=f'Handles per search call (default: {DEFAULT_BATCH_SIZE})')
    parser.add_argument('--no-searches', action='store_true',
                        help='Skip the `search:` topic queries from the accounts file')
    args = parser.parse_args()

    load_dotenv(override=True)

    try:
        validate_date_range(args.start_date, args.end_date)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if args.handles:
        handles = [h.strip() for h in args.handles.split(',') if h.strip()]
        searches = []
    else:
        # Imported lazily so this module stays usable if collect_x's optional
        # RSS dependencies (feedparser) aren't installed.
        from tools.collect_x import load_sources
        handle_pairs, searches = load_sources(args.accounts)
        handles = [h for h, _ in handle_pairs]

    print(f"Claude web search: {len(handles)} accounts, "
          f"{0 if args.no_searches else len(searches)} topic queries")

    articles = collect_accounts_via_claude(handles, args.start_date, args.end_date,
                                           batch_size=args.batch_size)

    if not args.no_searches:
        for query in searches:
            print(f"  [claude] topic search '{query}'...")
            found = collect_query_via_claude(query, args.start_date, args.end_date)
            print(f"    found {len(found)} item(s)")
            articles.extend(found)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Collected {len(articles)} item(s) via Claude web search")
    print(f"✓ Saved to {args.output}")


if __name__ == "__main__":
    main()
