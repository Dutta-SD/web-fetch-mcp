"""Function-oriented Strategy registry for the fetch tiers.

Each tier is an interchangeable ``async (request) -> FetchResult`` callable. They
are described uniformly by a :class:`Tier` record and held in a registry keyed by
mode name, so callers select or order tiers by name instead of branching on
hard-coded ``if mode == ...`` blocks. ``build_tier`` applies the retry decorator
at call time (``max_retries`` is a per-request argument).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from web_fetch_mcp.accessor.dynamic_client import fetch_dynamic
from web_fetch_mcp.accessor.static_client import fetch_static
from web_fetch_mcp.accessor.stealth_client import fetch_nodriver
from web_fetch_mcp.core.detection import is_blocked, looks_like_spa_shell
from web_fetch_mcp.core.models import FetchResult
from web_fetch_mcp.service.request import FetchRequest
from web_fetch_mcp.service.retry import Predicate, with_retry


def _not_blocked(result: FetchResult) -> bool:
    """Predicate: the result is usable content (not a block/challenge)."""
    return not is_blocked(result.body, result.status)


def _static_ok(result: FetchResult) -> bool:
    """Strict predicate for Tier 1 in auto mode: not blocked and not an SPA shell.

    An unrendered SPA shell is "not blocked" but still useless, so auto mode must
    escalate past it to a browser tier.
    """
    return not is_blocked(result.body, result.status) and not looks_like_spa_shell(
        result.body
    )


@dataclass(frozen=True, slots=True)
class Tier:
    """One interchangeable fetch strategy.

    Attributes:
        name: The mode name (``"static"``/``"dynamic"``/``"stealth"``).
        run: The async tier callable taking a :class:`FetchRequest`.
        predicate: The default satisfactoriness check for single-tier use.
    """

    name: str
    run: Callable[[FetchRequest], Awaitable[FetchResult]]
    predicate: Predicate


async def _run_static(req: FetchRequest) -> FetchResult:
    """Adapt :func:`fetch_static` to the uniform request-shaped tier signature."""
    return await fetch_static(req.url, proxy=req.proxy)


async def _run_dynamic(req: FetchRequest) -> FetchResult:
    """Adapt :func:`fetch_dynamic` to the uniform request-shaped tier signature."""
    return await fetch_dynamic(
        req.url, req.wait_ms, req.dismiss_selector, proxy=req.proxy
    )


async def _run_stealth(req: FetchRequest) -> FetchResult:
    """Adapt :func:`fetch_nodriver` to the uniform request-shaped tier signature."""
    return await fetch_nodriver(req.url, req.wait_ms, req.proxy)


# The registry: mode name -> Tier. The cheapest-first auto ladder is built from
# these in :mod:`web_fetch_mcp.service.escalation`.
TIERS: dict[str, Tier] = {
    "static": Tier("static", _run_static, _not_blocked),
    "dynamic": Tier("dynamic", _run_dynamic, _not_blocked),
    "stealth": Tier("stealth", _run_stealth, _not_blocked),
}


def build_tier(
    tier: Tier, max_retries: int, predicate: Predicate | None = None
) -> Callable[[FetchRequest], Awaitable[FetchResult | None]]:
    """Wrap a tier's runner in :func:`with_retry` for a given request budget.

    Args:
        tier: The strategy to run.
        max_retries: Per-tier retry budget for this request.
        predicate: Override for the tier's default predicate (auto mode uses the
            strict :func:`_static_ok` for the static link).

    Returns:
        An ``async (FetchRequest) -> FetchResult | None`` callable that retries
        and returns ``None`` when exhausted.
    """
    check = predicate or tier.predicate
    return with_retry(max_retries=max_retries, satisfactory=check)(tier.run)
