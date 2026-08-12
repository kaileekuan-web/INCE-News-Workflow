#!/usr/bin/env python3
"""
Deterministic grounding checks for generated summaries.

The failure mode this catches: a summary that states a figure the source never
contained — an invented raise, a made-up valuation, a percentage that came from
the model's priors rather than the article. Those are the errors that matter in
an investment brief, and they are the ones a reader is least able to spot.

This is deliberately NOT an LLM check. Asking a model to grade its own output is
both expensive and unreliable in the direction that matters (it tends to ratify).
Comparing numbers is arithmetic, so we do arithmetic.

The one real difficulty is that the summaries are Chinese while many sources are
English, so the same fact is written differently on each side:

    "$50 million"  ->  "5000万美元"      both normalize to 5.0e7
    "$1.2 billion" ->  "12亿美元"        both normalize to 1.2e9

So figures are parsed to a numeric value with unit multipliers applied, and a
summary figure counts as supported when the source contains a figure of the same
value. Comparing raw strings would flag every correctly converted number.

Scope is narrow on purpose — only figures worth being wrong about are checked:
money, percentages, and large counts. Small bare integers ("3 models") are
skipped because sources often spell them as words, which would produce noise
rather than signal. Bare years are skipped for the same reason.
"""

import re
from typing import List, Tuple

# Multipliers, longest-first so "万亿" wins over "万" and "billion" over "b".
_MULTIPLIERS = [
    ('万亿', 1e12), ('兆', 1e12),
    ('十亿', 1e9), ('亿', 1e8),
    ('百万', 1e6), ('千万', 1e7), ('万', 1e4), ('千', 1e3),
    ('trillion', 1e12), ('billion', 1e9), ('million', 1e6), ('thousand', 1e3),
    ('bn', 1e9), ('mm', 1e6),
    ('k', 1e3), ('m', 1e6), ('b', 1e9), ('t', 1e12),
]

_CURRENCY_CHARS = '$€£¥'
_CURRENCY_WORDS = ('美元', '元', '欧元', '英镑', '人民币', 'usd', 'eur', 'gbp', 'rmb', 'cny')

# A number, optionally preceded by a currency mark and followed by a unit.
# Unit letters are anchored with \b so "50 monkeys" doesn't read as "50m".
_UNIT_ALTERNATION = '|'.join(
    re.escape(u) + (r'\b' if u.isascii() else '') for u, _ in _MULTIPLIERS
)
_FIGURE_RE = re.compile(
    r'(?P<cur>[' + _CURRENCY_CHARS + r'])?\s*'
    r'(?P<num>\d[\d,]*(?:\.\d+)?)\s*'
    r'(?P<unit>' + _UNIT_ALTERNATION + r')?'
    r'\s*(?P<pct>%|percent\b|个百分点)?',
    re.IGNORECASE,
)

# URLs carry version numbers, ids and dates that are not claims.
_URL_RE = re.compile(r'https?://\S+')


def _iter_figures(text: str):
    """Yield (value, is_percent, has_currency, raw) for every number in text."""
    if not text:
        return
    text = _URL_RE.sub(' ', text)

    for m in _FIGURE_RE.finditer(text):
        raw = m.group(0).strip()
        try:
            value = float(m.group('num').replace(',', ''))
        except ValueError:
            continue

        unit = (m.group('unit') or '').lower()
        for name, mult in _MULTIPLIERS:
            if unit == name.lower():
                value *= mult
                break

        is_percent = bool(m.group('pct'))
        has_currency = bool(m.group('cur'))
        if not has_currency:
            # Currency written as a word after the unit: "5000万美元".
            tail = text[m.end():m.end() + 6].lower()
            head = text[max(0, m.start() - 6):m.start()].lower()
            has_currency = any(w in tail or w in head for w in _CURRENCY_WORDS)

        yield value, is_percent, has_currency, raw


def _is_checkable(value: float, is_percent: bool, has_currency: bool) -> bool:
    """
    Only figures worth being wrong about.

    Money and percentages always count. Bare numbers count once they are large
    enough to be a claim rather than a count — except years, which sources
    routinely imply ("today", "this week") rather than state.
    """
    if is_percent or has_currency:
        return True
    if 1900 <= value <= 2100 and float(value).is_integer():
        return False  # bare year
    return value >= 1000


def _matches(a: float, b: float) -> bool:
    """Equal within 1%, so 1.2e9 matches a source's 1,200,000,000."""
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) / scale <= 0.01


def unsupported_figures(summary: str, source: str) -> List[str]:
    """
    Return the figures asserted in `summary` that `source` does not support.

    Both sides are normalized to numeric values first, so a correctly converted
    "$50 million" -> "5000万美元" is supported, while an invented "6000万美元"
    is not. An empty list means every checkable figure traces back to the source.
    """
    source_figures = [
        (v, p) for v, p, _c, _r in _iter_figures(source)
    ]
    if not summary:
        return []

    missing, seen = [], set()
    for value, is_percent, has_currency, raw in _iter_figures(summary):
        if not _is_checkable(value, is_percent, has_currency):
            continue
        if any(is_percent == sp and _matches(value, sv) for sv, sp in source_figures):
            continue
        if raw in seen:
            continue
        seen.add(raw)
        missing.append(raw)

    return missing


def check_summary(summary: str, source: str) -> Tuple[bool, List[str]]:
    """(is_grounded, unsupported_figures) — convenience wrapper."""
    missing = unsupported_figures(summary, source)
    return (not missing), missing


if __name__ == "__main__":
    # Run: python tools/grounding.py
    cases = [
        # (summary, source, expected_unsupported_count, note)
        ("公司完成5000万美元A轮融资。", "The company raised $50 million in its Series A.", 0,
         "correct unit conversion is supported"),
        ("公司完成6000万美元A轮融资。", "The company raised $50 million in its Series A.", 1,
         "wrong amount is caught"),
        ("估值达12亿美元。", "valued at $1.2 billion", 0, "billion/亿 conversion"),
        ("用户增长30%。", "grew 30% year over year", 0, "percent match"),
        ("用户增长50%。", "grew 30% year over year", 1, "wrong percent caught"),
        ("公司发布了3款新模型。", "The company released three new models.", 0,
         "small counts are not checked (sources spell them as words)"),
        ("2026年发布。", "Released this week.", 0, "bare years are not checked"),
        ("融资1.5亿美元，估值10亿美元。", "raised $150 million", 1,
         "one supported, one invented"),
        ("公司有200万用户。", "The company has 2 million users.", 0, "large counts convert"),
        ("详见 https://x.com/a/status/1954321", "no numbers here", 0,
         "URL digits are ignored"),
    ]
    failures = 0
    for summary, source, expected, note in cases:
        missing = unsupported_figures(summary, source)
        ok = len(missing) == expected
        failures += (not ok)
        print(f"{'PASS' if ok else 'FAIL'}  {note}")
        if not ok:
            print(f"      expected {expected} unsupported, got {len(missing)}: {missing}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    raise SystemExit(1 if failures else 0)
