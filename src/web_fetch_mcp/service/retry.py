"""Async retry/backoff decorator.

``with_retry`` wraps an async tier callable so that it is retried on transient
failures with exponential backoff and full jitter, preferring a server-supplied
``Retry-After`` over the computed delay. It is hand-rolled on the stdlib helpers
in :mod:`web_fetch_mcp.core.backoff` rather than a third-party library
(``tenacity`` etc.) because the retry condition is domain-specific — it hinges on
a satisfactoriness predicate over a :class:`FetchResult` and on a header parsed
from that result — and because the decorator is small, fully tested, and a
deliberate demonstration of the pattern.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable

from web_fetch_mcp.core.backoff import backoff_delay, retry_after_delay
from web_fetch_mcp.core.detection import is_blocked
from web_fetch_mcp.core.models import FetchResult

log = logging.getLogger("web-fetch")

Predicate = Callable[[FetchResult], bool]
TierCallable = Callable[..., Awaitable[FetchResult]]


def with_retry(
    *, max_retries: int, satisfactory: Predicate
) -> Callable[[TierCallable], Callable[..., Awaitable[FetchResult | None]]]:
    """Decorate an async tier callable with retry + backoff.

    The wrapped callable is invoked up to ``max_retries + 1`` times. Between
    attempts it sleeps for the response's ``Retry-After`` (when present) else an
    exponentially-backed-off, fully-jittered delay.

    Args:
        max_retries: Number of retries after the first attempt (``0`` = one try).
        satisfactory: Predicate over the returned :class:`FetchResult`; ``True``
            means "good content, stop".

    Returns:
        A decorator that turns ``async (...) -> FetchResult`` into
        ``async (...) -> FetchResult | None`` — yielding the satisfactory result,
        or ``None`` when every attempt is exhausted (the caller then escalates).
    """

    def decorate(fn: TierCallable) -> Callable[..., Awaitable[FetchResult | None]]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> FetchResult | None:
            for attempt in range(max_retries + 1):
                try:
                    result = await fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001 — network/launch error
                    log.info("%s attempt %d errored: %s", fn.__name__, attempt, e)
                    if attempt < max_retries:
                        await asyncio.sleep(backoff_delay(attempt))
                        continue
                    return None
                if satisfactory(result):
                    return result
                log.info(
                    "%s attempt %d unsatisfactory (status=%s, blocked=%s)",
                    fn.__name__,
                    attempt,
                    result.status,
                    is_blocked(result.body, result.status),
                )
                if attempt < max_retries:
                    ra = retry_after_delay(result.headers)
                    await asyncio.sleep(ra if ra is not None else backoff_delay(attempt))
            return None

        return wrapper

    return decorate
