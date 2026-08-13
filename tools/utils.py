#!/usr/bin/env python3
"""
Shared utility functions for AI news collection

Functions:
- Date parsing and validation
- Deduplication
- Text cleaning
- Normalization
"""

import json
import os
from datetime import datetime
from urllib.parse import urlparse
from typing import Tuple, List, Dict, Any

# Every inference in this pipeline goes through the Anthropic API. One key
# (ANTHROPIC_API_KEY in .env) covers summarization, categorization, funding
# extraction, translation, insights, and the debate agents.
#
# NEWS_CLAUDE_MODEL overrides the model for the whole pipeline — useful when a run is
# large enough that Sonnet's price is worth the quality trade (a full weekly run
# is ~150-250 calls). Use the exact IDs: claude-opus-5, claude-sonnet-5,
# claude-haiku-4-5.
# The env vars are NEWS_-prefixed on purpose: a bare CLAUDE_EFFORT is already set
# inside a Claude Code shell, and an unprefixed name would let the pipeline
# silently inherit it when run from there.
CLAUDE_MODEL = os.getenv('NEWS_CLAUDE_MODEL', 'claude-opus-5')

# Effort controls how much the model thinks before answering. These are short
# structured tasks (summarize one article, extract one JSON object), so 'low' is
# the default: it is the main cost and latency lever, and on Opus 5 it holds up
# well on exactly this kind of mechanical work. Raise to 'medium'/'high' via
# NEWS_CLAUDE_EFFORT if summaries start reading thin.
CLAUDE_EFFORT = os.getenv('NEWS_CLAUDE_EFFORT', 'low')

# Floor for max_tokens. On Opus 5 thinking is ON by default and max_tokens caps
# thinking AND response text together, so a budget sized for the answer alone
# truncates the answer mid-sentence. Callers ask for what the answer needs; this
# floor is the thinking headroom on top.
CLAUDE_MAX_TOKENS = int(os.getenv('NEWS_CLAUDE_MAX_TOKENS', '4096'))

_claude_client = None
_claude_client_lock = __import__('threading').Lock()

# Per-request timeout for ordinary (non-search) calls. The client's own 600s
# default is sized for web-search turns, which legitimately run for minutes; a
# stuck article summary holding a worker for ten minutes is not the same thing.
# Summaries normally return in 5-15s, so 180s is already generous.
CLAUDE_TIMEOUT = int(os.getenv('NEWS_CLAUDE_TIMEOUT', '180'))


def get_claude_client():
    """
    Return a shared Anthropic client, or None if the SDK or key is missing.

    Cached at module level so a 200-article run reuses one HTTP connection pool
    instead of building a client per call.
    """
    global _claude_client
    if _claude_client is not None:
        return _claude_client

    # Calls now run concurrently, so several threads can reach this at once on
    # the first batch. Without the lock they each build a client and race to
    # assign the global — harmless but wasteful, and it defeats the shared
    # connection pool this cache exists for.
    with _claude_client_lock:
        if _claude_client is not None:
            return _claude_client

        try:
            import anthropic
        except ImportError:
            print("  WARNING: anthropic package not installed. Run: pip install anthropic")
            return None

        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            print("  WARNING: ANTHROPIC_API_KEY not set in .env")
            return None

        # max_retries covers 429/5xx with exponential backoff, which is what the old
        # hand-rolled `time.sleep(20 * attempt)` loops were doing by hand.
        _claude_client = anthropic.Anthropic(api_key=api_key, max_retries=5, timeout=600.0)
    return _claude_client


def call_claude(prompt: str, model: str = None, max_tokens: int = None,
                system: str = None, effort: str = None, timeout: int = None) -> str:
    """
    Call Claude with a single user-turn prompt and return its text.

    Args:
        prompt: The user-turn prompt.
        model: Model ID override (default: CLAUDE_MODEL).
        max_tokens: Budget for the answer. Raised to at least CLAUDE_MAX_TOKENS
                    because thinking shares this budget on Opus 5 — see the
                    CLAUDE_MAX_TOKENS note above.
        system: Optional system prompt.
        effort: 'low' | 'medium' | 'high' | 'xhigh' | 'max' (default: CLAUDE_EFFORT).
        timeout: Per-request timeout in seconds (default: the client's 600s).

    Returns the model's text response, or '' on any failure (missing key, rate
    limit exhaustion, safety refusal, network error) so every call site can fall
    back to the raw description exactly as it did before.
    """
    client = get_claude_client()
    if client is None:
        return ''

    budget = max(max_tokens or 0, CLAUDE_MAX_TOKENS)
    client = client.with_options(timeout=float(timeout or CLAUDE_TIMEOUT))

    kwargs = {
        'model': model or CLAUDE_MODEL,
        'max_tokens': budget,
        'output_config': {'effort': effort or CLAUDE_EFFORT},
        'messages': [{'role': 'user', 'content': prompt}],
    }
    if system:
        kwargs['system'] = system

    try:
        response = client.messages.create(**kwargs)
    except Exception as e:
        print(f"  WARNING: Claude call failed: {e}")
        return ''

    # Safety classifiers can decline a request: HTTP 200, empty/partial content.
    # Check before reading content, or an indexing error masks the real cause.
    if response.stop_reason == 'refusal':
        category = getattr(getattr(response, 'stop_details', None), 'category', None)
        print(f"  WARNING: Claude declined this request (category: {category})")
        return ''

    text = ''.join(b.text for b in response.content if b.type == 'text').strip()

    if response.stop_reason == 'max_tokens':
        print(f"  WARNING: Claude hit max_tokens ({budget}) — output may be truncated")

    return text


# Anthropic's server-side web search tool. Versioned by date, so it lives here as
# one constant rather than being retyped at each call site — when the version
# moves, this is the only line that changes.
WEB_SEARCH_TOOL_TYPE = os.getenv('NEWS_WEB_SEARCH_TOOL', 'web_search_20260209')


def call_claude_search(prompt: str, schema: dict = None, max_uses: int = 8,
                       effort: str = 'medium', max_tokens: int = 8192,
                       model: str = None, max_resumes: int = 3,
                       label: str = 'search'):
    """
    Run a prompt with Claude's server-side web search and return the result.

    Claude issues the queries and reads the results on Anthropic's side, so
    there is no search API to configure here.

    Args:
        schema: JSON Schema for the reply. When given, the API is constrained to
                emit matching JSON and this returns the parsed object — no regex
                scraping of the response text, which is what used to drift
                whenever the model changed how it wrapped its output.
        max_uses: Cap on searches per call, so one bad query can't run away.
        effort: Search-and-reconcile is judgment work; 'medium' is the floor
                that behaves well here, above the per-article default.
        max_resumes: A long search turn can stop with `pause_turn` and ask to be
                resumed. Resume at most this many times, then give up.
        label: Name used in warnings, so failures say which search failed.

    Returns the parsed object (with schema), the reply text (without), or None
    on any failure — every caller treats None as "no results" and carries on.
    """
    client = get_claude_client()
    if client is None:
        return None

    tools = [{"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": max_uses}]
    output_config = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}

    messages = [{"role": "user", "content": prompt}]

    try:
        response = client.messages.create(
            model=model or CLAUDE_MODEL,
            max_tokens=max_tokens,
            output_config=output_config,
            tools=tools,
            messages=messages,
        )

        resumes = 0
        while response.stop_reason == 'pause_turn' and resumes < max_resumes:
            resumes += 1
            response = client.messages.create(
                model=model or CLAUDE_MODEL,
                max_tokens=max_tokens,
                output_config=output_config,
                tools=tools,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response.content},
                ],
            )
        if response.stop_reason == 'pause_turn':
            print(f"  WARNING: {label} still paused after {max_resumes} resumes — partial results")
    except Exception as e:
        print(f"  WARNING: {label} failed: {e}")
        return None

    if response.stop_reason == 'refusal':
        print(f"  WARNING: Claude declined the {label} request")
        return None

    text = ''.join(b.text for b in response.content if b.type == 'text').strip()
    if not text:
        return None

    if not schema:
        return text

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Constrained decoding makes this near-impossible; if it ever fires it
        # means the turn was cut short (max_tokens) rather than that the model
        # freelanced the format.
        print(f"  WARNING: {label} returned unparseable JSON ({e})")
        return None


def translate_to_chinese_claude(text: str) -> str:
    """
    Translate text to Simplified Chinese with Claude.

    The proper-noun rule is load-bearing: an untethered translation pass will
    transliterate or "correct" company and product names (an early run turned
    ByteDance into "BiteDance"), which reads as a factual error in the report.
    Translation must not add, drop, or reinterpret anything either — it is the
    one step in the pipeline with no new information to contribute.
    """
    if not text:
        return ''
    prompt = (
        "将以下文本翻译成简体中文。\n\n"
        "要求：\n"
        "- 公司名、产品名、模型名、人名等专有名词保持原文拼写，不要音译、缩写或改写"
        "（例如 ByteDance、Claude Opus、Sam Altman 原样保留）。\n"
        "- 数字、金额、比例、日期与原文完全一致。\n"
        "- 只翻译，不要增补背景信息、不要解释、不要省略任何内容。\n"
        "- 只输出翻译结果，不要其他说明。\n\n"
        f"{text}"
    )
    return call_claude(prompt, max_tokens=2048)


# How many API calls run at once. The pipeline's wall-clock time was almost
# entirely one-request-at-a-time waiting: a bi-weekly run makes ~150 Claude
# calls of 5-15s each, plus one web search per day in the range, and did them
# strictly in sequence.
#
# 6 is deliberately modest. These are long requests, not a burst, and the SDK
# already retries 429s with backoff — the goal is to stop idling on the network,
# not to find the rate limit. Raise with NEWS_MAX_WORKERS if your account's
# limits allow; set it to 1 to get the old sequential behaviour back when
# debugging, which also makes the log readable again.
MAX_WORKERS = max(1, int(os.getenv('NEWS_MAX_WORKERS', '6')))


def parallel_map(fn, items: list, workers: int = None, label: str = '',
                 on_result=None) -> list:
    """
    Run `fn` over `items` concurrently, returning results in input order.

    Order preservation is the point: every caller here writes the results back
    into a list or a document where position is meaningful, so a plain
    as-completed loop would quietly shuffle the report.

    An exception in one item does not abort the batch — it is reported and that
    slot comes back as None, matching how the sequential code treated a failed
    call. `on_result(index, item, result)` fires as each finishes, under a lock,
    for progress printing and incremental saving.
    """
    if not items:
        return []
    workers = workers or MAX_WORKERS
    if workers <= 1 or len(items) == 1:
        results = []
        for i, item in enumerate(items):
            try:
                r = fn(item)
            except Exception as exc:
                print(f"  WARNING: {label or 'task'} failed for item {i}: {exc}")
                r = None
            results.append(r)
            if on_result:
                on_result(i, item, r)
        return results

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    results = [None] * len(items)
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                print(f"  WARNING: {label or 'task'} failed for item {i}: {exc}")
                results[i] = None
            if on_result:
                with lock:
                    on_result(i, items[i], results[i])
    return results


def validate_date_range(start_date: str, end_date: str) -> Tuple[datetime, datetime]:
    """
    Validate date format and range

    Args:
        start_date: Date string in YYYY-MM-DD format
        end_date: Date string in YYYY-MM-DD format

    Returns:
        Tuple of (start_datetime, end_datetime)

    Raises:
        ValueError if invalid
    """
    try:
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    except ValueError as e:
        raise ValueError(f"Dates must be in YYYY-MM-DD format: {e}")

    if start_dt > end_dt:
        raise ValueError("Start date must be before end date")

    if (end_dt - start_dt).days > 365:
        raise ValueError("Date range cannot exceed 1 year")

    return start_dt, end_dt


_STOP_WORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or', 'with', 'by',
    'from', 'its', 'it', 'this', 'that', 'as', 'up', 'out', 'new',
    'has', 'have', 'had', 'will', 'would', 'could', 'can', 'may',
}


def _title_words(title: str) -> frozenset:
    """Normalize a title to a word set for similarity comparison."""
    import re
    words = re.sub(r'[^a-z0-9\s]', ' ', title.lower()).split()
    return frozenset(w for w in words if w not in _STOP_WORDS and len(w) > 1)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _content_length(article: Dict[str, Any]) -> int:
    return len(article.get('content') or article.get('description') or '')


def deduplicate_articles(articles: List[Dict[str, Any]],
                         similarity_threshold: float = 0.5) -> List[Dict[str, Any]]:
    """
    Remove duplicate articles by URL and by title similarity.

    Two articles are considered duplicates when their Jaccard word-set
    similarity exceeds similarity_threshold (default 0.5). When duplicates
    are found the article with more content is kept, so the richer version
    of the same story survives.
    """
    seen_urls: set = set()
    accepted: list = []        # (article, title_words) pairs kept so far

    for article in articles:
        url = article.get('url', '')
        title = article.get('title', '')

        # Exact URL match — always a duplicate
        if url and url in seen_urls:
            continue

        words = _title_words(title)

        # Check semantic similarity against every already-accepted article
        duplicate_idx = None
        for i, (_, kept_words) in enumerate(accepted):
            if _jaccard(words, kept_words) >= similarity_threshold:
                duplicate_idx = i
                break

        if duplicate_idx is not None:
            # Keep whichever version has richer content
            kept_article, kept_words = accepted[duplicate_idx]
            if _content_length(article) > _content_length(kept_article):
                accepted[duplicate_idx] = (article, words)
                if kept_article.get('url'):
                    seen_urls.discard(kept_article['url'])
                if url:
                    seen_urls.add(url)
        else:
            accepted.append((article, words))
            if url:
                seen_urls.add(url)

    return [art for art, _ in accepted]


def clean_text(text: str) -> str:
    """
    Clean and normalize text

    Args:
        text: Input text

    Returns:
        Cleaned text
    """
    if not text:
        return ''

    # Remove extra whitespace
    text = ' '.join(text.split())

    # Remove common artifacts
    text = text.replace('\xa0', ' ')   # Non-breaking space
    text = text.replace('\u200b', '')  # Zero-width space
    text = text.replace('\r', '')      # Carriage return

    return text.strip()


def extract_domain(url: str) -> str:
    """
    Extract domain from URL

    Args:
        url: Full URL

    Returns:
        Domain name (e.g., 'techcrunch.com')
    """
    try:
        parsed = urlparse(url)
        return parsed.netloc
    except Exception:
        return ''


def format_date_for_display(date_str: str) -> str:
    """
    Format ISO date string for display

    Args:
        date_str: ISO 8601 date string (e.g., '2026-01-15T10:30:00Z')

    Returns:
        Formatted date string (e.g., '2026-01-15')
    """
    try:
        # Handle various ISO formats
        if 'T' in date_str:
            date_str = date_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(date_str)
        else:
            dt = datetime.strptime(date_str, '%Y-%m-%d')

        return dt.strftime('%Y-%m-%d')
    except Exception:
        # If parsing fails, return original
        return date_str


def normalize_article(article: Dict[str, Any], source: str) -> Dict[str, Any]:
    """
    Normalize article to standard format

    Args:
        article: Raw article dictionary
        source: Source name (e.g., 'TechCrunch')

    Returns:
        Normalized article dictionary
    """
    return {
        'source': source,
        'title': clean_text(article.get('title', '')),
        'description': clean_text(article.get('description', '')),
        'url': article.get('url', ''),
        'published_at': article.get('published_at', ''),
        'content': clean_text(article.get('content', '')),
        'raw': article  # Keep original for debugging
    }


if __name__ == "__main__":
    # Test functions
    print("Testing utils.py...")

    # Test date validation
    try:
        start, end = validate_date_range('2026-01-01', '2026-01-15')
        print(f"Date range valid: {start} to {end}")
    except ValueError as e:
        print(f"Date validation error: {e}")

    # Test deduplication
    test_articles = [
        {'url': 'https://example.com/article1', 'title': 'Test Article'},
        {'url': 'https://example.com/article1', 'title': 'Test Article'},  # Duplicate
        {'url': 'https://example.com/article2', 'title': 'Another Article'},
    ]
    unique = deduplicate_articles(test_articles)
    print(f"Deduplication: {len(test_articles)} -> {len(unique)} articles")

    # Test text cleaning
    dirty_text = "  Text with\xa0 extra   spaces\u200b  "
    clean = clean_text(dirty_text)
    print(f"Text cleaning: '{dirty_text}' -> '{clean}'")

    print("All tests passed!")
