#!/usr/bin/env python3
"""
Find the news article behind a link-less X post.

Most announcement tweets link nowhere — on a real two-week run only 2 of 20
surviving posts carried a link. Those items could only ever appear as an
unlinked headline summarized from 200 characters of tweet, which is neither
professional nor in-depth. This step closes that gap: it asks Claude, with its
server-side web search, which published article covers the same event, then
**verifies the answer before accepting it**.

Verification is the point of this tool. A web search will happily return a
plausible article about the right company and the wrong event, and a link that
doesn't support the summary underneath it is worse than no link at all. So each
proposed URL is fetched and scored against the post's own words
(`news_filters.corroborates`); anything below the threshold is discarded and the
post stays unlinked rather than wrongly linked.

What a resolved article buys, beyond the link: `summarize_articles.py` fetches
it and summarizes *the article* instead of the tweet, so the entry gets the
figures, investors and product detail the post left out.

Input:  .tmp/classified_articles.json   (after dedup, before summarization)
Output: same file, with source_url / source_title / source_publisher filled in

Usage:
    python tools/resolve_sources.py
    python tools/resolve_sources.py --input .tmp/classified_articles.json --yes
    python tools/resolve_sources.py --no-verify      # trust the search (not advised)
"""

import os
import sys
import json
import argparse
from datetime import datetime
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import call_claude_search, parallel_map, MAX_WORKERS
from tools.news_filters import (
    article_text, news_link, is_x_url, corroborates, canonical_url, dedupe,
    article_subject, has_primary_source, is_own_announcement, clean_headline,
)


# ── Upgrading a source to the primary one ─────────────────────────────────────

PRIMARY_UPGRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "string",
            "description": "The company or organisation the story is ABOUT "
                           "(not the publication reporting it)",
        },
        "url": {
            "type": "string",
            "description": "That organisation's OWN announcement of this news — "
                           "their blog post, /news, /press or newsroom entry, "
                           "model card, research page, or their press release on "
                           "a wire. Empty string if none exists.",
        },
    },
    "required": ["subject", "url"],
    "additionalProperties": False,
}


def _find_primary_for_article(article: dict) -> tuple:
    """
    Find the announcement behind one news item. Returns (url, subject) or ('','').

    Distinct from resolve_sources() above, which finds *an* article for a post
    that links nowhere. This one starts from an item that already has a link and
    asks a different question: who is this story about, and did they announce it
    themselves? "Cursor open-sources MoK" surfaces MarkTechPost; "Cursor's own
    blog post about MoK" surfaces Cursor.
    """
    title = clean_headline(article.get('title') or '')
    summary = (article.get('summary') or article.get('description') or '')[:400]
    current = news_link(article)

    prompt = (
        f"Find the ORIGINAL announcement behind this AI news item — the source "
        f"the coverage is based on, not the coverage.\n\n"
        f"Headline: {title}\n"
        f"Summary: {summary}\n"
        f"Currently linked (a secondary report): {current}\n"
        f"Posted by X account: @{(article.get('author') or '').lstrip('@')}\n\n"
        f"First identify the company or organisation the story is ABOUT. Then find "
        f"THEIR own publication of this news, in this order:\n"
        f"1. Their blog, /news, /press, /research or newsroom entry about it.\n"
        f"2. Their model card or repository page (Hugging Face, GitHub) when the "
        f"news is a model or open-source release.\n"
        f"3. Their press release on Business Wire, PR Newswire or GlobeNewswire.\n"
        f"4. For a policy or regulatory story, the issuing body's own page — the "
        f"regulator, agency or standards group that published it.\n\n"
        f"Return an empty url if no such announcement exists. An empty answer is "
        f"correct and useful. Do NOT substitute another news article, a newsletter, "
        f"a roundup, an aggregator (TechCrunch, The Verge, VentureBeat, "
        f"MarktechPost, Business Insider) or a syndicated reprint — those are what "
        f"this search exists to replace.\n\n"
        f"The URL must be about THIS specific news, not the organisation's homepage."
    )

    result = call_claude_search(prompt, schema=PRIMARY_UPGRADE_SCHEMA, max_uses=4,
                                label=f"primary source: {title[:40]}")
    if not result:
        return '', ''
    url = (result.get('url') or '').strip()
    subject = (result.get('subject') or '').strip()
    if not url:
        return '', ''
    # Verify against the subject's own name rather than a list of known sites:
    # the model returns trade press here too when asked for a primary source.
    if is_own_announcement(url, subject) or is_own_announcement(
            url, article_subject(article)):
        return url, subject
    return '', ''


def upgrade_to_primary_sources(articles: list) -> list:
    """
    Replace aggregator links with the subject's own announcement where one exists.

    Records `subject_company` on every article it resolves, so a later
    has_primary_source() check does not have to guess the subject from the X
    handle. Articles that already link to a primary source are left untouched
    and cost nothing.
    """
    needs = [a for a in articles if news_link(a) and not has_primary_source(a)]
    if not needs:
        print("  Sources: every article already links to a primary source")
        return articles

    print(f"  Sources: {len(needs)}/{len(articles)} article(s) link to aggregators "
          f"— searching for the original announcements ({MAX_WORKERS} at a time)")

    found = parallel_map(_find_primary_for_article, needs, label='primary source')

    resolved = 0
    for article, (url, subject) in zip(needs, found):
        if url:
            article['source_url'] = url
            article['source_publisher'] = subject or article.get('source_publisher', '')
            if subject:
                article['subject_company'] = subject
            resolved += 1

    print(f"    Upgraded {resolved}/{len(needs)} to a primary source")
    return articles

# Posts per search call. One per call wastes a search turn on an item the model
# resolves in a sentence; too many and it rations a fixed search budget across
# them and returns nothing for the ones at the end.
DEFAULT_BATCH_SIZE = 4

# A search turn plus the verification fetch, per batch. Used only for the cost
# estimate printed before the run.
COST_PER_BATCH_USD = 0.05

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The id of the post this resolves, exactly as given",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL of the published article covering this event, "
                                       "or empty string if no article covers it",
                    },
                    "title": {
                        "type": "string",
                        "description": "Headline of that article, or empty string",
                    },
                    "publisher": {
                        "type": "string",
                        "description": "Publication name, e.g. TechCrunch, or empty string",
                    },
                    "covers_same_event": {
                        "type": "boolean",
                        "description": "true only if the article reports the SAME event as "
                                       "the post, not merely the same company",
                    },
                },
                "required": ["id", "url", "title", "publisher", "covers_same_event"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["resolutions"],
    "additionalProperties": False,
}


def _prompt(batch: list) -> str:
    """Build the search prompt for one batch of (id, date, author, text) items."""
    lines = []
    for item in batch:
        lines.append(
            f"id {item['id']} — posted {item['date']} by {item['author'] or 'unknown'}:\n"
            f"\"{item['text']}\""
        )
    posts = '\n\n'.join(lines)

    return f"""For each social post below, find the published news article that reports the SAME event.

{posts}

What counts as a match:
- The article reports the same event as the post — the same funding round, the same product launch, the same acquisition. An article about the same company at a different time is NOT a match.
- Published around the post's date. A much older article is a different event.

Which source to return — the primary one, always:
- FIRST look for the announcement itself: the company's own blog post, /news or /press page, its press release (Business Wire, PR Newswire, GlobeNewswire), its research paper or model card, or the investor's own post about the round. That is what the report should link to.
- Only if no primary source exists, return trade-press coverage (TechCrunch, The Verge, VentureBeat, Bloomberg, Reuters, The Information), and then anything else credible.
- A TechCrunch writeup of a launch the company blogged about is the wrong answer when the blog post exists. Aggregator coverage is a report of the announcement; the report wants the announcement. Look for it before settling.
- Deal databases (Crunchbase, PitchBook, Dealroom, Tracxn) are compiled from announcements and are never primary — use them to find the announcement, not as the link.

Rules:
- NEVER return an x.com, twitter.com or nitter URL. Those are the post itself, not coverage of it. If the only thing you can find is the post, return an empty url for that id.
- An empty url is a correct and useful answer. Do not stretch to fill one — a wrong link is worse than no link, and every url you return is checked against the post's own wording before it is used.
- Set covers_same_event true only when you are confident the article reports that specific event.
- Return exactly one entry per id listed above, in the same order."""


def _select_unresolved(articles: list) -> list:
    """Indices of articles that have no usable link yet."""
    return [i for i, a in enumerate(articles) if not news_link(a)]


def _fetch_for_verification(url: str) -> str:
    # Imported here so this module stays importable without bs4 installed when
    # running with --no-verify.
    from tools.summarize_articles import fetch_article_content
    return fetch_article_content(url)


def resolve_sources(articles: list, batch_size: int = DEFAULT_BATCH_SIZE,
                    verify: bool = True, threshold: float = 0.45,
                    max_items: int = None) -> dict:
    """
    Fill in `source_url` for articles that have no link. Mutates `articles`.

    Returns a stats dict: attempted / proposed / verified / rejected.
    """
    pending = _select_unresolved(articles)
    if max_items:
        pending = pending[:max_items]

    stats = {'attempted': len(pending), 'proposed': 0, 'verified': 0,
             'rejected_unverified': 0, 'rejected_x_link': 0, 'no_article': 0}
    if not pending:
        print("  Every post already links to its source — nothing to resolve")
        return stats

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    print(f"  {len(pending)} post(s) with no link → {len(batches)} search call(s) "
          f"({MAX_WORKERS} at a time)")

    def _search(batch_idx: list):
        batch = [{
            'id': i,
            'date': (articles[i].get('published_at') or '')[:10],
            'author': articles[i].get('author', ''),
            'text': article_text(articles[i])[:600],
        } for i in batch_idx]
        return call_claude_search(_prompt(batch), schema=RESOLUTION_SCHEMA,
                                  max_uses=10, label='source resolution')

    # Batches are independent web searches; running them one at a time was
    # 30-60s of waiting each. Verification (a page fetch per accepted link)
    # stays in the sequential pass below, where the accept/reject log reads in
    # order and the stats have a single writer.
    searched = parallel_map(_search, batches, label='source resolution')

    for n, (batch_idx, result) in enumerate(zip(batches, searched), 1):
        resolutions = (result or {}).get('resolutions') or []
        by_id = {r.get('id'): r for r in resolutions if isinstance(r, dict)}

        for i in batch_idx:
            article = articles[i]
            res = by_id.get(i)
            label = article_text(article)[:60]

            if not res or not (res.get('url') or '').strip():
                stats['no_article'] += 1
                continue
            url = res['url'].strip()
            if not url.startswith('http'):
                stats['no_article'] += 1
                continue
            if is_x_url(url):
                # The model was asked not to do this; when it does anyway, an
                # x.com link is exactly what this whole change exists to avoid.
                stats['rejected_x_link'] += 1
                print(f"      ✗ {label}… → returned an X link, discarded")
                continue
            if not res.get('covers_same_event'):
                stats['no_article'] += 1
                continue

            stats['proposed'] += 1

            if verify:
                page = _fetch_for_verification(url)
                ok, score = corroborates(article_text(article), page, threshold)
                if not ok:
                    stats['rejected_unverified'] += 1
                    reason = 'page unreadable' if len(page) < 200 else f'overlap {score}'
                    print(f"      ✗ {label}… → {reason}, discarded")
                    continue
                article['source_verified'] = True
                article['source_match_score'] = score

            article['source_url'] = url
            article['source_title'] = (res.get('title') or '').strip()
            article['source_publisher'] = (res.get('publisher') or '').strip()
            article['source_resolved'] = 'claude-web-search'
            stats['verified'] += 1
            publisher = article['source_publisher'] or canonical_url(url).split('/')[2]
            print(f"      ✓ {label}… → {publisher}")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Find and verify the news article behind link-less posts')
    parser.add_argument('--input', default='.tmp/classified_articles.json')
    parser.add_argument('--output', default=None,
                        help='Defaults to --input (the file is updated in place)')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
                        help=f'Posts per search call (default: {DEFAULT_BATCH_SIZE})')
    parser.add_argument('--max', type=int, default=None,
                        help='Only try the first N unlinked posts (for a cheap trial run)')
    parser.add_argument('--threshold', type=float, default=0.45,
                        help='Share of the post\'s distinctive words that must appear in '
                             'the article for the link to be accepted (default: 0.45)')
    parser.add_argument('--no-verify', dest='verify', action='store_false',
                        help='Accept the search result without fetching and checking the '
                             'article. Faster, and how wrong links get in.')
    parser.add_argument('--yes', '-y', action='store_true', help='Skip the cost prompt')
    args = parser.parse_args()

    load_dotenv(override=True)

    if not os.getenv('ANTHROPIC_API_KEY'):
        print("ERROR: ANTHROPIC_API_KEY not found in .env — this step needs web search")
        sys.exit(1)

    with open(args.input, encoding='utf-8') as f:
        articles = json.load(f)

    linked_before = sum(1 for a in articles if news_link(a))
    print(f"Loaded {len(articles)} articles — {linked_before} already link to a source")

    pending = _select_unresolved(articles)
    if args.max:
        pending = pending[:args.max]
    n_batches = (len(pending) + args.batch_size - 1) // args.batch_size
    estimate = n_batches * COST_PER_BATCH_USD
    print(f"Estimated cost: ${estimate:.2f} ({n_batches} search call(s))")
    if estimate > 2.0 and not args.yes:
        if input(f"Continue? (y/n): ").lower() != 'y':
            print("Cancelled")
            sys.exit(0)

    stats = resolve_sources(articles, args.batch_size, args.verify,
                            args.threshold, args.max)

    # Resolution can reveal duplicates that were invisible before it: two posts
    # about one event, from two accounts, in different words, now both pointing
    # at the same article. Collapsing them here rather than at document
    # generation means the duplicate is never summarized — the dedup is free,
    # the LLM call it saves is not.
    before = len(articles)
    articles, dedup_stats = dedupe(articles)
    if len(articles) < before:
        detail = ', '.join(f"{k}={v}" for k, v in dedup_stats.items() if v)
        print(f"\n  Post-resolution dedup removed {before - len(articles)} "
              f"duplicate(s): {detail}")

    output = args.output or args.input
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    linked_after = sum(1 for a in articles if news_link(a))
    print(f"\n✓ Resolved {stats['verified']} of {stats['attempted']} unlinked post(s)")
    if stats['rejected_unverified']:
        print(f"  {stats['rejected_unverified']} proposed link(s) discarded — the article "
              f"did not corroborate the post")
    if stats['rejected_x_link']:
        print(f"  {stats['rejected_x_link']} X link(s) discarded")
    if stats['no_article']:
        print(f"  {stats['no_article']} post(s) have no published coverage")
    print(f"  Source links: {linked_after}/{len(articles)} articles "
          f"(was {linked_before})")
    print(f"✓ Saved to {output}")


if __name__ == '__main__':
    main()
