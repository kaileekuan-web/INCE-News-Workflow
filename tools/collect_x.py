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

Reliability: public Nitter instances are frequently rate-limited or down. The
collector tries several in order and moves on when one fails, then reports a
health line (served / unserved / empty) so a low yield is explainable rather
than silent. Override the instance list with NITTER_INSTANCES (comma-separated).

When Nitter comes up short, `collect_x_claude.py` fills the gap using Claude's
server-side web search — see `--fallback`. X stays the primary source either
way: the fallback searches for what the same accounts announced, and its items
are labelled `X/Twitter (via Claude search)` so provenance is visible downstream.

What this collector is FOR shapes what it keeps (see tools/news_filters.py):
smaller AI startups, reporting real events. Posts about the companies listed in
`frontier_labs.txt` and posts that are somebody's take rather than news are
dropped here, where they cost nothing, instead of downstream where each one is
an LLM call. `--include-frontier` / `--include-opinion` turn that off.

Every post also carries `source_url`: the link the tweet points at, which is
what reports hyperlink. The x.com URL is kept as `x_url` for provenance and is
never rendered as a link.

Output: .tmp/raw_x.json  (standard article schema, source='X/Twitter')
"""

import os
import re
import sys
import json
import time
import argparse
from datetime import datetime
from urllib.parse import quote_plus
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.utils import clean_text, validate_date_range, parallel_map, MAX_WORKERS
from tools.news_filters import (
    is_ai_relevant, is_newsworthy, is_promo, is_opinion, has_hard_event,
    extract_source_url, frontier_match, get_labs, news_link, dedupe,
    _MONEY_RE,
)

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
#
# Verified 2026-08-09:
#   nitter.net           200, full RSS            <- primary
#   rss.xcancel.com      200 (xcancel.com 302s here)
#   nitter.poast.org     403
#   lightbrd.com         403
#   nitter.tiekoetter.com  bot-check interstitial
#   nitter.privacydev.net  no response
# The dead ones are kept last as cheap fallbacks in case they come back.
DEFAULT_INSTANCES = [
    "https://nitter.net",
    "https://rss.xcancel.com",
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://lightbrd.com",
    "https://nitter.privacydev.net",
]

# Nitter prefixes self-replies / thread continuations with "R to @handle:".
_REPLY_PREFIX_RE = re.compile(r'^R to @[A-Za-z0-9_]+:\s*')

# Tweets shorter than this after cleanup are almost always image-only posts,
# bare links, or one-word replies — not worth an LLM call.
MIN_TWEET_CHARS = 60

# Pause between account fetches. Public Nitter instances rate-limit aggressively
# and answer with an empty-but-valid feed rather than an error, so pacing the
# requests is the difference between ~80 posts and ~15 on a 30-account list.
REQUEST_DELAY_SECONDS = float(os.getenv('X_REQUEST_DELAY', '2'))

# Per-request timeout for a Nitter mirror. A healthy instance answers in about a
# second; one that has not answered in eight is not going to. At the previous
# 15s, a run where several mirrors were down spent minutes waiting on hosts that
# were never going to reply — with six instances tried per account, that is the
# slowest part of collection whenever the list has rotted.
FETCH_TIMEOUT_SECONDS = float(os.getenv('X_FETCH_TIMEOUT', '8'))

# ── Selectivity ───────────────────────────────────────────────────────────────
# Every post kept here becomes an LLM call downstream, so filtering is a cost
# control as much as a quality one. The vocabulary all four levels are built on
# lives in tools/news_filters.py, shared with the document generators.
#
# Levels are set per section of x_accounts.txt with
# `#filter: off | on | strict | news`:
#
#   off    — keep everything (official company accounts; already on-topic)
#   on     — must mention AI (founders and researchers, who also post personal
#            takes, conference photos and jokes)
#   strict — must mention AI AND report an event, not comment on one (VCs, who
#            post a high volume of opinion around a small amount of signal)
#   news   — strict, plus no opinion markers at all ("my take", "hot take").
#            The right level for a source that mixes reporting and punditry.
#
# Independent of level, two rules apply to every account (both defeatable with
# a CLI flag): frontier labs and big tech are dropped, and posts carrying an
# explicit opinion marker are dropped.
FILTER_LEVELS = ('off', 'on', 'strict', 'news')

_STATUS_RE = re.compile(r'(?:twitter\.com|x\.com|/)([A-Za-z0-9_]+)/status/(\d+)')
_STRIP_URLS_RE = re.compile(r'https?://\S+')
_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/120.0 Safari/537.36'),
}

# Instances disagree on what a legitimate client looks like: nitter.net wants a
# browser UA, while xcancel answers a full browser UA with 400 "This URL only
# works inside an RSS client". Try both before writing an instance off.
_USER_AGENTS = [
    _HEADERS['User-Agent'],
    'Mozilla/5.0 (compatible; RSS reader)',
]


def get_instances() -> list:
    override = os.getenv('NITTER_INSTANCES', '').strip()
    if override:
        return [i.strip().rstrip('/') for i in override.split(',') if i.strip()]
    return DEFAULT_INSTANCES


def load_sources(path: str) -> tuple:
    """
    Parse x_accounts.txt into (handles, search_queries).

    `handles` is a list of (handle, filter_level) pairs. The level is set by
    `#filter: off | on | strict` directive lines and applies to every handle
    below it (default off). See FILTER_LEVELS for what each one means.
    """
    handles, searches = [], []
    if not os.path.exists(path):
        print(f"WARNING: {path} not found — no X sources to collect")
        return handles, searches

    level = 'off'
    with open(path, 'r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                directive = line.lstrip('#').strip().lower()
                if directive.startswith('filter:'):
                    value = directive.split(':', 1)[1].strip()
                    # Accept the old boolean spellings so existing files keep working
                    if value in ('true', 'yes'):
                        value = 'on'
                    elif value in ('false', 'no'):
                        value = 'off'
                    if value in FILTER_LEVELS:
                        level = value
                    else:
                        print(f"  WARNING: {path}:{lineno} unknown filter level "
                              f"'{value}' — expected one of {', '.join(FILTER_LEVELS)}")
                continue
            if line.lower().startswith('search:'):
                q = line.split(':', 1)[1].strip()
                if q:
                    searches.append(q)
            else:
                handles.append((line.lstrip('@').strip(), level))
    return handles, searches


def _fetch_feed(path: str, instances: list, failures: dict = None, max_failures: int = 3):
    """
    Fetch an RSS path (e.g. '/OpenAI/rss') across instances until one works.

    An instance that is rate-limiting serves a structurally valid RSS document
    with a proper <title> and ZERO <item>s — indistinguishable from a genuinely
    quiet timeline if you only check that the channel parsed. Accepting that as
    success silently zeroed out every account after the limit kicked in, because
    the caller then pinned the throttled instance as its known-good one.

    So: a feed with entries wins immediately. An empty-but-valid feed is only
    returned once every other instance has also failed to produce entries.

    `failures` counts hard failures (no response / non-200 / unparseable) per
    instance so a genuinely dead one stops being retried for every handle. An
    empty-but-valid feed is neutral — it may just be a quiet account.
    """
    empty_feed = empty_base = None

    for base in instances:
        if failures is not None and failures.get(base, 0) >= max_failures:
            continue

        url = f"{base}{path}"
        responded = False
        for user_agent in _USER_AGENTS:
            try:
                resp = requests.get(url, headers={'User-Agent': user_agent},
                                    timeout=FETCH_TIMEOUT_SECONDS)
                if resp.status_code != 200 or not resp.content:
                    continue
                feed = feedparser.parse(resp.content)
                responded = True
                if feed.entries:
                    if failures is not None:
                        failures[base] = 0
                    return feed, base
                if empty_feed is None and feed.feed and feed.feed.get('title'):
                    empty_feed, empty_base = feed, base
            except (requests.Timeout, requests.ConnectionError):
                # The host is unreachable or too slow. The second user agent
                # exists for instances that answer but dislike a browser UA —
                # it cannot help here, and retrying doubled the cost of every
                # dead mirror. With six instances in the list that was the
                # difference between 16 and 96 seconds of waiting per account.
                break
            except Exception:
                continue

        if failures is not None and not responded:
            failures[base] = failures.get(base, 0) + 1

    return empty_feed, empty_base


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


def passes_filter(text: str, level: str, allow_opinion: bool = False,
                  topic_assured: bool = False) -> tuple:
    """
    Decide whether a post survives its account's filter level.

    Returns (keep: bool, reason: str) — the reason drives the per-account
    rejection tally so a surprising drop in volume is explainable rather than
    silent.

    The opinion check runs at every level including `off`: an official company
    account posting "our take on why agents win" is still a take, and the report
    is for news. Pass allow_opinion=True (CLI: --include-opinion) to keep them.

    `topic_assured` skips the "must mention AI" test for items whose topic was
    fixed by how they were found. It exists because that test has one bad false
    negative: "Sierra raised a $350M Series C led by Greenoaks" never says AI,
    and a funding round at an AI startup is the most valuable item this report
    can carry. Claude web-search results answered an AI-specific query, so the
    topic is already established; a raw timeline post has no such guarantee and
    still has to say it.
    """
    if is_promo(text):
        return False, 'promo'
    if not allow_opinion:
        opinionated, _ = is_opinion(text)
        if opinionated:
            return False, 'opinion'
    if level == 'off':
        return True, ''
    if not topic_assured and not is_ai_relevant(text):
        # A funding round is the most valuable item this report can carry, and
        # the wording of one routinely never says "AI": "Harvey raised a $300
        # million Series E led by Kleiner Perkins" says what happened and not
        # what the company does. Requiring the word here silently discarded
        # exactly those posts.
        #
        # So a hard event with a real figure survives the topic test — and is
        # marked, because a VC also posts non-AI rounds. `topic_unverified`
        # travels with the article to the summarizer, which reads the full
        # article and can answer "is this an AI company?" as a regex never
        # could; anything it calls 非AI is dropped before the report.
        if has_hard_event(text) and _MONEY_RE.search(text):
            return True, 'topic-unverified'
        return False, 'not-ai'
    if level in ('strict', 'news') and not is_newsworthy(text):
        return False, 'commentary'
    return True, ''


def _normalize_entry(entry, source_label: str, handle_hint: str = '') -> dict:
    text = _REPLY_PREFIX_RE.sub('', clean_text(entry.get('title', '')))
    url = _canonical_url(entry)
    # Author handle: prefer the URL, fall back to the feed author or hint.
    m = _STATUS_RE.search(url)
    handle = m.group(1) if m else (entry.get('author', '') or handle_hint).lstrip('@')
    dt = _entry_datetime(entry)

    title = f"@{handle}: {text}" if handle else text
    if len(title) > 140:
        title = title[:137] + '...'

    # The link the post points at — the actual news, as opposed to the tweet
    # about it. Reports link here; the x.com URL below is kept only as
    # provenance, and is never rendered as a link. Nitter's description carries
    # the same links as the title but as HTML, so the title is enough.
    source_url = extract_source_url(text)

    return {
        'source': source_label,
        'title': clean_text(title),
        'description': text,
        'url': url,
        'source_url': source_url,   # the news behind the post ('' if it links nowhere)
        'x_url': url,               # provenance only — never linked in the report
        'published_at': (dt.isoformat() + 'Z') if dt else '',
        'content': text,          # full tweet text — lets summarizer score without scraping x.com
        'author': f"@{handle}" if handle else '',
        'raw': {'nitter_link': entry.get('link', '')},
    }


def _fingerprint(body: str) -> str:
    """Normalized leading text, used to collapse near-duplicate posts."""
    return re.sub(r'\W+', '', body.lower())[:80]


def collect_x(start_date: str, end_date: str, accounts_file: str,
              max_per_account: int = 8, fallback: str = 'auto',
              min_posts: int = 20, include_frontier: bool = False,
              include_opinion: bool = False) -> list:
    """
    Collect X/Twitter posts, preferring Nitter RSS and falling back to Claude.

    Nitter is the high-fidelity path: it returns the actual tweet text. It is
    also the fragile one — public instances rot, rate-limit, and answer with
    empty-but-valid feeds. `fallback` decides what happens when that goes wrong:

        auto   (default) run the Claude web-search fallback for the accounts
                Nitter produced nothing for, but only when the run looks
                degraded — some account went unserved, or the whole run came
                back under `min_posts`. A healthy run costs nothing extra.
        always run the fallback for every account that produced nothing, even
                when the run looks healthy. Widest coverage, highest cost.
        never   Nitter only. Use when you want zero API spend in this step, and
                accept that an outage means an empty report.

    `search:` topic queries are always routed through Claude unless fallback is
    'never': public Nitter instances stopped serving search some time ago, so
    those lines were silently returning nothing on every run.

    Two report-wide rules are applied here rather than downstream, because a
    post dropped here is an LLM call not spent on it:

        include_frontier=False (default) drops anything about a company in
            frontier_labs.txt — the report is about smaller AI startups.
        include_opinion=False (default) drops posts carrying an explicit
            opinion marker at any filter level.
    """
    start_dt, end_dt = validate_date_range(start_date, end_date)
    # Include the whole end day.
    end_dt = end_dt.replace(hour=23, minute=59, second=59)

    instances = get_instances()
    handles, searches = load_sources(accounts_file)
    print(f"X/Twitter sources: {len(handles)} accounts, {len(searches)} searches")
    print(f"Nitter instances: {', '.join(instances)}")

    articles = []
    seen_urls = set()
    seen_texts = set()
    # Per-account Nitter outcome, so a zero is explainable rather than silent:
    #   served   — a feed with entries came back (the account may still yield 0
    #              posts after date filtering, which is a real quiet account)
    #   empty    — a structurally valid feed with no entries at all: either a
    #              genuinely dormant account or a throttled instance
    #   unserved — no instance answered for this handle
    account_status = {}
    # Hard-failure tally per instance, shared across every fetch in this run.
    instance_failures = {}
    # Why posts were dropped, aggregated across the whole run.
    rejects = {'promo': 0, 'not-ai': 0, 'commentary': 0, 'opinion': 0,
               'frontier': 0, 'too-short': 0, 'retweet': 0, 'duplicate': 0,
               'over-cap': 0}
    # Kept, not rejected: an event with a figure but no AI wording. Counted
    # separately so it never reads as a rejection in the run log.
    unverified = [0]
    # Which companies the frontier filter removed, so the drop is explainable.
    frontier_hits = {}
    # Posts kept per handle — drives which accounts the fallback chases.
    yielded = {}
    labs = [] if include_frontier else get_labs()

    def frontier_reject(article) -> bool:
        """Drop (and tally) a post about a frontier lab / big tech company."""
        if include_frontier:
            return False
        name, why = frontier_match(article, labs)
        if not name:
            return False
        rejects['frontier'] += 1
        frontier_hits[name] = frontier_hits.get(name, 0) + 1
        return True

    def add_entries(feed, source_label, level, handle_hint='', cap=None):
        added = 0
        for entry in feed.entries:
            title = entry.get('title', '') or ''
            # Skip retweets — Nitter prefixes them "RT by @user:".
            if title.startswith('RT by '):
                rejects['retweet'] += 1
                continue
            dt = _entry_datetime(entry)
            if dt is None or not (start_dt <= dt <= end_dt):
                continue

            art = _normalize_entry(entry, source_label, handle_hint)
            if not art['url'] or art['url'] in seen_urls:
                continue

            # Drop image-only posts, bare links and one-word replies — they cost
            # an LLM call downstream and carry no summarizable content.
            body = _STRIP_URLS_RE.sub('', art['description']).strip()
            if len(body) < MIN_TWEET_CHARS:
                rejects['too-short'] += 1
                continue

            keep, reason = passes_filter(body, level, allow_opinion=include_opinion)
            if not keep:
                rejects[reason] += 1
                continue
            if reason == 'topic-unverified':
                # Kept on the strength of its event, not its topic. The
                # summarizer decides whether it is an AI company at all.
                art['topic_unverified'] = True
                unverified[0] += 1

            # Frontier labs and big tech are out of scope for this report. The
            # check runs on the whole article (author, linked domain, opening
            # words), not just the body, so a lab's own account is caught even
            # when the post text never names it.
            if frontier_reject(art):
                continue

            # Accounts often post the same announcement twice (a thread head and
            # a standalone version). Collapse them on normalized leading text.
            fingerprint = _fingerprint(body)
            if fingerprint in seen_texts:
                rejects['duplicate'] += 1
                continue

            # Cap is applied last so it counts posts that actually survived,
            # not raw feed position — otherwise a burst of promo tweets at the
            # top of a timeline would eat the whole quota for that account.
            if cap is not None and added >= cap:
                rejects['over-cap'] += 1
                continue

            seen_urls.add(art['url'])
            seen_texts.add(fingerprint)
            articles.append(art)
            added += 1
        return added

    # Instances are always tried in configured (quality) order.
    #
    # An earlier version promoted whichever instance last succeeded to the front
    # of the list. That backfired badly: @xai 404s on nitter.net, so the fallback
    # mirror served it and got promoted — and that mirror returns single-entry
    # truncated feeds, so every account after @xai silently collected nothing.
    # Never let a fallback win the top slot just by answering once.

    # 1) Account timelines. Space the requests out — hammering a public Nitter
    # instance with 30+ back-to-back fetches is what trips its rate limiter.
    for i, (handle, level) in enumerate(handles):
        if i:
            time.sleep(REQUEST_DELAY_SECONDS)
        feed, base = _fetch_feed(f"/{handle}/rss", instances, instance_failures)
        if feed is None:
            account_status[handle] = 'unserved'
            print(f"  [skip] @{handle}: no Nitter instance served this account")
            continue
        account_status[handle] = 'served' if feed.entries else 'empty'
        n = add_entries(feed, 'X/Twitter', level=level,
                        handle_hint=handle, cap=max_per_account)
        yielded[handle] = n
        tag = '' if level == 'off' else f' [{level}]'
        note = '' if feed.entries else '  (empty feed — dormant account or throttled instance)'
        print(f"  @{handle}: {n} posts in range{tag}{note}")

    # 2) Tweet searches over Nitter. Kept because an instance that still serves
    # search is strictly better than a web search (real tweets, exact matches),
    # but public instances have disabled it broadly — the Claude fallback below
    # is what actually covers these queries now. Results are uncurated, so they
    # always get the strictest filter.
    search_hits = {}
    for query in searches:
        time.sleep(REQUEST_DELAY_SECONDS)
        path = f"/search/rss?f=tweets&q={quote_plus(query)}"
        feed, base = _fetch_feed(path, instances, instance_failures)
        if feed is None:
            search_hits[query] = 0
            print(f"  [skip] search '{query}': no instance served results (search may be disabled)")
            continue
        n = add_entries(feed, 'X/Twitter (search)', level='strict',
                        cap=max_per_account)
        search_hits[query] = n
        print(f"  search '{query}': {n} posts in range [strict]")

    dropped = ', '.join(f"{k}={v}" for k, v in rejects.items() if v)
    if dropped:
        print(f"  Filtered out: {dropped}")
    if unverified[0]:
        print(f"  Kept on event alone: {unverified[0]} post(s) report a funding "
              f"event without saying AI — the summarizer checks whether the "
              f"company is one")
    if frontier_hits:
        ranked = sorted(frontier_hits.items(), key=lambda kv: -kv[1])
        print("  Frontier/big-tech coverage dropped: "
              + ', '.join(f"{name} ({n})" for name, n in ranked))

    # ── Nitter health ─────────────────────────────────────────────────────────
    unserved = [h for h, s in account_status.items() if s == 'unserved']
    empty = [h for h, s in account_status.items() if s == 'empty']
    served = len(handles) - len(unserved)
    print(f"\nNitter health: {served}/{len(handles)} accounts served, "
          f"{len(unserved)} unserved, {len(empty)} empty feed(s) — "
          f"{len(articles)} posts kept")
    if unserved:
        print(f"  Unserved: {', '.join('@' + h for h in unserved)}")

    def finish(collected: list) -> list:
        """
        Last pass before the JSON is written: collapse duplicates across the
        whole run and report how many posts carry a link to real coverage.

        The per-account dedup above only catches identical wording. This one
        also collapses two accounts linking to the same article and two
        write-ups of the same event — see news_filters.dedupe.
        """
        unique, stats = dedupe(collected)
        removed = ', '.join(f"{k}={v}" for k, v in stats.items() if v)
        if removed:
            print(f"  Cross-source dedup removed {len(collected) - len(unique)} "
                  f"post(s): {removed}")
        linked = sum(1 for a in unique if news_link(a))
        print(f"  Source links: {linked}/{len(unique)} posts link to the news "
              f"behind them (the rest render without a link — never to x.com)")
        return unique

    if fallback == 'never':
        if unserved or len(articles) < min_posts:
            print("  Fallback disabled (--fallback never) — collecting Nitter results only")
        return finish(articles)

    # ── Claude fallback ───────────────────────────────────────────────────────
    # Only chase accounts that actually came back with nothing. An account that
    # yielded posts is already covered at higher fidelity, and re-searching it
    # would cost a search turn to produce duplicates.
    barren = [h for h, _ in handles if yielded.get(h, 0) == 0]
    degraded = bool(unserved) or len(articles) < min_posts
    run_accounts = barren and (fallback == 'always' or (fallback == 'auto' and degraded))
    pending_searches = [q for q in searches if not search_hits.get(q)]

    if not run_accounts and not pending_searches:
        return finish(articles)

    from tools.collect_x_claude import (
        collect_accounts_via_claude,
        collect_query_via_claude,
    )

    def absorb(found: list, level: str) -> int:
        """Filter and dedupe fallback items into `articles`. Returns kept count."""
        kept = 0
        for art in found:
            if not art['url'] or art['url'] in seen_urls:
                rejects['duplicate'] += 1
                continue
            body = _STRIP_URLS_RE.sub('', art['description']).strip()
            if len(body) < MIN_TWEET_CHARS:
                rejects['too-short'] += 1
                continue
            # Fallback items go through the same content filters as Nitter
            # posts, so a relaxed source can't smuggle promo or commentary past
            # the per-account selectivity levels.
            keep, reason = passes_filter(body, level, allow_opinion=include_opinion,
                                         topic_assured=True)
            if not keep:
                rejects[reason] += 1
                continue
            if reason == 'topic-unverified':
                art['topic_unverified'] = True
            if frontier_reject(art):
                continue
            fingerprint = _fingerprint(body)
            if fingerprint in seen_texts:
                rejects['duplicate'] += 1
                continue
            seen_urls.add(art['url'])
            seen_texts.add(fingerprint)
            articles.append(art)
            kept += 1
        return kept

    if run_accounts:
        reason = 'unserved accounts' if unserved else f'under {min_posts} posts'
        print(f"\nClaude web-search fallback ({reason}): "
              f"{len(barren)} account(s) with no Nitter results")
        # Filter levels are per-account; the fallback returns a mixed batch, so
        # it is filtered at the strictest level present among the accounts asked
        # for. Erring strict keeps a fallback run from loosening the bar.
        levels = {lvl for h, lvl in handles if h in set(barren)}
        level = 'strict' if 'strict' in levels else ('on' if 'on' in levels else 'off')
        found = collect_accounts_via_claude(barren, start_date, end_date)
        kept = absorb(found, level)
        print(f"  Fallback added {kept} post(s) (filter: {level})")

    if pending_searches:
        # One web-search turn per topic, and they were run back to back. On a
        # real run that was minutes of pure waiting — the slowest thing left in
        # collection once Nitter is healthy. absorb() still runs sequentially
        # afterwards: it mutates the shared seen_urls/seen_texts sets, and the
        # per-query log has to stay readable.
        print(f"\nClaude web-search fallback for {len(pending_searches)} topic(s) "
              f"({MAX_WORKERS} at a time)...")
        results = parallel_map(
            lambda q: collect_query_via_claude(q, start_date, end_date),
            pending_searches, label='topic search')
        for query, found in zip(pending_searches, results):
            kept = absorb(found or [], 'strict')
            print(f"  '{query}': {kept} post(s) kept [strict]")

    return finish(articles)


def main():
    parser = argparse.ArgumentParser(description='Collect AI X/Twitter posts via Nitter RSS')
    parser.add_argument('--start_date', required=True, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end_date', required=True, help='End date (YYYY-MM-DD)')
    parser.add_argument('--accounts', default='x_accounts.txt',
                        help='Path to accounts/searches file (default: x_accounts.txt)')
    parser.add_argument('--output', default='.tmp/raw_x.json', help='Output file path')
    parser.add_argument('--max-per-account', type=int, default=8,
                        help='Cap surviving posts per account (default: 8). '
                             'Each kept post costs one LLM call downstream.')
    parser.add_argument('--fallback', choices=['auto', 'always', 'never'], default='auto',
                        help="Claude web-search fallback for accounts Nitter didn't serve: "
                             "auto (default, only when the run looks degraded), "
                             "always, or never (Nitter only, no API spend here)")
    parser.add_argument('--min-posts', type=int, default=20,
                        help='Below this many posts, --fallback auto treats the run as '
                             'degraded and calls Claude (default: 20)')
    parser.add_argument('--include-frontier', action='store_true',
                        help='Keep posts about the companies in frontier_labs.txt '
                             '(OpenAI, Anthropic, big tech …). Off by default: this '
                             'report is about smaller AI startups.')
    parser.add_argument('--include-opinion', action='store_true',
                        help="Keep posts carrying an opinion marker ('my take', "
                             "'unpopular opinion' …). Off by default: the report is news.")
    args = parser.parse_args()

    load_dotenv(override=True)

    try:
        validate_date_range(args.start_date, args.end_date)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    articles = collect_x(args.start_date, args.end_date, args.accounts,
                         max_per_account=args.max_per_account,
                         fallback=args.fallback, min_posts=args.min_posts,
                         include_frontier=args.include_frontier,
                         include_opinion=args.include_opinion)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    via_claude = sum(1 for a in articles if a.get('via_claude_search'))
    detail = f" ({len(articles) - via_claude} via Nitter, {via_claude} via Claude search)" if via_claude else ""
    print(f"\n✓ Collected {len(articles)} X/Twitter posts{detail}")
    print(f"✓ Saved to {args.output}")

    if not articles:
        # Exit non-zero so the pipeline stops here with an accurate cause,
        # rather than three phases later with an empty document.
        print("\nERROR: 0 posts collected. Nitter may be down/rate-limited and the "
              "Claude fallback found nothing.\n"
              "  - check ANTHROPIC_API_KEY is set (the fallback needs it)\n"
              "  - try --fallback always, a wider date range, or set NITTER_INSTANCES in .env")
        sys.exit(1)


if __name__ == "__main__":
    main()
