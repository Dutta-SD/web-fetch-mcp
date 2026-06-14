"""Transitional compatibility shim (removed in the final refactor step).

The real implementation now lives in the layered ``web_fetch_mcp`` package:

    controller -> service -> accessor -> core

This module re-exports the handful of symbols the legacy ``from server import ...``
test imports still reach for, so the suite stays green while the tests are
migrated to the package paths (plan step 8). It is deleted in step 9 once the
tests import from ``web_fetch_mcp`` directly and the console entry point
(``web-fetch-mcp`` -> ``web_fetch_mcp.controller.app:main``) is the way to run.
"""

from __future__ import annotations

# Controller (FastMCP app + tools).
from web_fetch_mcp.controller.app import fetch, main, mcp, screenshot

# Core domain symbols still imported by the test suite.
from web_fetch_mcp.core.backoff import normalize_selectors as _normalize_selectors
from web_fetch_mcp.core.backoff import retry_after_delay as _retry_after_delay
from web_fetch_mcp.core.config import RETRY_AFTER_CAP as _RETRY_AFTER_CAP
from web_fetch_mcp.core.detection import is_blocked as _is_blocked
from web_fetch_mcp.core.models import FetchBlocked, FetchResult
from web_fetch_mcp.core.models import FetchResult as _FetchResult
from web_fetch_mcp.core.rendering import detect_content_type as _detect_content_type
from web_fetch_mcp.core.rendering import render_by_type as _render_by_type
from web_fetch_mcp.core.rendering import to_output as _to_output

# Orchestration shim still imported by the test suite (rewritten in step 8).
from web_fetch_mcp.service.retry import with_retry as _with_retry


async def _attempt(coro_factory, satisfactory, max_retries: int) -> _FetchResult | None:
    """Transitional shim over ``with_retry`` (removed once tests migrate, step 8).

    Adapts the historical zero-arg-factory call shape to the decorator: the
    factory is wrapped and immediately invoked.

    Args:
        coro_factory: Zero-arg callable returning a fresh awaitable -> FetchResult.
        satisfactory: Predicate over the FetchResult; True means 'stop'.
        max_retries: Retries after the first attempt.

    Returns:
        The satisfactory FetchResult, or None when exhausted.
    """
    runner = _with_retry(max_retries=max_retries, satisfactory=satisfactory)(coro_factory)
    return await runner()


__all__ = [
    "FetchBlocked",
    "FetchResult",
    "_RETRY_AFTER_CAP",
    "_attempt",
    "_detect_content_type",
    "_is_blocked",
    "_normalize_selectors",
    "_render_by_type",
    "_retry_after_delay",
    "_to_output",
    "fetch",
    "main",
    "mcp",
    "screenshot",
]
