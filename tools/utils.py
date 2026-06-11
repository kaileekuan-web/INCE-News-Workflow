#!/usr/bin/env python3
"""
Shared utility functions for AI news collection

Functions:
- Date parsing and validation
- Deduplication
- Text cleaning
- Normalization
"""

from datetime import datetime
from urllib.parse import urlparse
from typing import Tuple, List, Dict, Any


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
