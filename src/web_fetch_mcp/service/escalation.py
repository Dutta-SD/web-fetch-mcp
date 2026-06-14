"""Cheapest-first escalation across the fetch tiers.

Applies the Chain-of-Responsibility *intent*: walk an ordered list of tier
strategies and return the first satisfactory result, escalating past tiers that
come back blocked/insufficient. It is a plain ordered loop over the Strategy
registry rather than a formal linked-handler chain — three tiers do not warrant
successor-pointer machinery.
"""

from __future__ import annotations

import logging

from web_fetch_mcp.core.models import FetchBlocked, FetchResult
from web_fetch_mcp.service.request import FetchRequest
from web_fetch_mcp.service.strategies import TIERS, Tier, _static_ok, build_tier

log = logging.getLogger("web-fetch")


def build_auto_chain(dismiss_selector: str | list[str] | None) -> list[Tier]:
    """Build the ordered tier chain for ``auto`` mode.

    The static tier cannot click overlays, so when a ``dismiss_selector`` is
    requested it is dropped from the chain and escalation starts at the browser
    tier.

    Args:
        dismiss_selector: The request's dismiss selector(s), if any.

    Returns:
        The ordered list of tiers to try.
    """
    if dismiss_selector:
        return [TIERS["dynamic"], TIERS["stealth"]]
    return [TIERS["static"], TIERS["dynamic"], TIERS["stealth"]]


async def escalate(
    request: FetchRequest, *, max_retries: int, chain: list[Tier]
) -> FetchResult:
    """Try each tier in order, returning the first satisfactory result.

    The static tier (when first in the chain) uses the strict predicate so an
    unrendered SPA shell escalates to a browser tier.

    Args:
        request: The fetch request.
        max_retries: Per-tier retry budget.
        chain: The ordered tiers to attempt.

    Returns:
        The first satisfactory :class:`FetchResult`.

    Raises:
        FetchBlocked: If every tier in the chain was blocked or failed.
    """
    for tier in chain:
        predicate = _static_ok if tier.name == "static" else None
        runner = build_tier(tier, max_retries, predicate=predicate)
        result = await runner(request)
        if result is not None:
            return result
        log.info("tier %s insufficient; escalating", tier.name)
    tried = ", ".join(t.name for t in chain)
    raise FetchBlocked(
        f"all strategies ({tried}) blocked/failed for {request.url}. "
        f"Try a residential proxy or, for CAPTCHA-gated sites, a managed unblocker API."
    )
