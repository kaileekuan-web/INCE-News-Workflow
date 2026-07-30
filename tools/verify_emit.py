"""
Emits funding-event claims to the shared verification service.

Every funding event here was pulled from a scraped news article (or an
OpenAI web-search result) -- never a document news-workflow itself produced
-- so these are external_facts claims (provenance_type=web), not extraction
claims: the point is independent re-corroboration of what the source
article said, not just checking that we transcribed it correctly.

source_ref is the article's own URL. Events with no url can't be checked
against anything and are skipped rather than submitted with a placeholder.

This is supplementary: a failure to submit claims must never break report
generation. Failures are logged loudly (never swallowed silently) but don't
propagate past this module.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

try:
    from ince_claim_envelope import EnvelopeRejected, emit_many
except ImportError:  # verification service package not installed in this env
    EnvelopeRejected = None  # type: ignore
    emit_many = None  # type: ignore

_UNKNOWN = {"", "不详", "n/a", "na", "none", "null", "unknown"}


def _known(value: Any) -> bool:
    return bool(value) and str(value).strip().lower() not in _UNKNOWN


def _entity_ref(company_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    return f"entity:{slug or 'unknown-company'}"


def _retrieved_at(event: dict) -> datetime:
    date_str = (event.get("date") or "")[:10]
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _claims_from_event(event: dict) -> list[tuple[str, str, float]]:
    """Returns (claim_type, claim_text, materiality) tuples for the
    checkable facts in one funding event. Fields still marked unknown
    ('不详' / 'N/A' / etc, see detect_funding.py / generate_word_doc.py's
    normalization) are skipped -- there's nothing to verify in a blank."""
    company = event.get("company")
    if not _known(company):
        return []
    out: list[tuple[str, str, float]] = []

    stage = event.get("stage")
    if _known(stage):
        out.append(("funding_stage", f"{company}'s funding round is {stage}.", 0.5))

    amount = event.get("raise")
    if _known(amount):
        if _known(stage):
            out.append(("funding_amount", f"{company} raised {amount} in its {stage} round.", 0.8))
        else:
            out.append(("funding_amount", f"{company} raised {amount}.", 0.8))

    valuation = event.get("valuation")
    if _known(valuation):
        out.append(("funding_amount", f"{company} was valued at {valuation}.", 0.7))

    investors = event.get("investors")
    if _known(investors):
        investor_text = ", ".join(investors) if isinstance(investors, list) else str(investors)
        if _known(investor_text):
            out.append(("key_people", f"{company}'s investors include {investor_text}.", 0.5))

    return out


def emit_funding_claims(events: list[dict]) -> None:
    if emit_many is None:
        logger.warning(
            "ince_claim_envelope not installed -- %d funding event(s) were not "
            "submitted to the verification service. Add '-e ../verification-service/envelope' "
            "to requirements.txt and reinstall.",
            len(events),
        )
        return

    now = datetime.now(timezone.utc)
    envelopes = []
    skipped_no_url = 0

    for event in events:
        url = event.get("url")
        if not _known(url):
            skipped_no_url += 1
            continue

        subject_ref = _entity_ref(event.get("company", ""))
        retrieved_at = _retrieved_at(event)
        retrieved_at = min(retrieved_at, now)  # a stale/bad date must never validate as "in the future"

        for claim_type, claim_text, materiality in _claims_from_event(event):
            envelopes.append({
                "claim_id": str(uuid.uuid4()),
                "tool_id": "news-workflow",
                "subject_ref": subject_ref,
                "claim_text": claim_text,
                "claim_type": claim_type,
                "check_family": "external_facts",
                "provenance_type": "web",
                "source_ref": url,
                "retrieved_at": retrieved_at,
                "materiality": materiality,
            })

    if skipped_no_url:
        logger.info("Skipped %d funding event(s) with no source url -- nothing to verify against", skipped_no_url)

    if not envelopes:
        return

    try:
        emit_many(envelopes)
        logger.info("Submitted %d funding claims to verification service", len(envelopes))
    except EnvelopeRejected as exc:
        logger.error(
            "One or more funding claims were rejected by the verification "
            "service (report generation continues regardless): %s", exc,
        )
    except Exception:
        logger.exception(
            "Could not reach the verification service to submit funding claims "
            "(report generation continues regardless)",
        )
