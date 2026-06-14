"""Backoff timing and small stateless helpers used by the retry machinery."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from web_fetch_mcp.core.config import BACKOFF_BASE, BACKOFF_CAP, RETRY_AFTER_CAP


def retry_after_delay(headers: dict) -> float | None:
    """Parse a ``Retry-After`` header into a capped, non-negative wait.

    Args:
        headers: Lower-cased response headers.

    Returns:
        The wait in seconds (capped at :data:`RETRY_AFTER_CAP`), or ``None`` when
        the header is absent or unparseable. Accepts both integer-seconds and
        HTTP-date forms; a past date yields ``0.0``.
    """
    raw = headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return min(float(raw), RETRY_AFTER_CAP)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - datetime.now(UTC)).total_seconds()
    return min(max(delta, 0.0), RETRY_AFTER_CAP)


def backoff_delay(attempt: int) -> float:
    """Compute an exponential backoff delay with full jitter.

    Args:
        attempt: The zero-based attempt index.

    Returns:
        A random delay in ``[0, min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt))``.
    """
    raw = min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt))
    return random.uniform(0, raw)


def normalize_selectors(sel) -> list[str]:
    """Normalize a dismiss/expand selector argument to a list of strings.

    Args:
        sel: ``None``, a single selector string, or an iterable of selectors.

    Returns:
        A list of selector strings (empty when ``sel`` is ``None``).
    """
    if sel is None:
        return []
    if isinstance(sel, str):
        return [sel]
    return list(sel)
