"""The ``fetch_url`` facade: the framework-free entry point for fetching.

Validates arguments, dispatches to a single tier or the auto escalation chain,
and renders every result through one content-type-aware path. The controller's
``fetch`` tool is a thin wrapper over this.
"""

from __future__ import annotations

from web_fetch_mcp.core.models import FetchBlocked
from web_fetch_mcp.core.rendering import render_by_type
from web_fetch_mcp.service.escalation import build_auto_chain, escalate
from web_fetch_mcp.service.request import FetchRequest
from web_fetch_mcp.service.strategies import TIERS, build_tier

VALID_MODES = frozenset({"auto", "static", "dynamic", "stealth"})
VALID_OUTPUTS = frozenset({"markdown", "html", "text", "article"})


async def fetch_url(
    url: str,
    mode: str = "auto",
    output: str = "markdown",
    wait_ms: int = 2000,
    dismiss_selector: str | None = None,
    proxy: str | None = None,
    max_retries: int = 1,
) -> str:
    """Fetch a URL and render it in the requested output format.

    In ``auto`` mode the request escalates cheapest-first across the tiers; the
    single-tier modes run exactly one. Every result — from any tier — is rendered
    through :func:`render_by_type`, so JSON/PDF/image handling applies uniformly
    (browser tiers always classify as HTML and fall through to markdown/text).

    Args:
        url: The fully-qualified URL.
        mode: ``"auto"`` (default), ``"static"``, ``"dynamic"`` or ``"stealth"``.
        output: ``"markdown"`` (default), ``"article"``, ``"text"`` or ``"html"``.
        wait_ms: Extra settle time (ms) for the browser tiers.
        dismiss_selector: Overlay selector(s) to click; forces a browser tier.
        proxy: Optional proxy URL.
        max_retries: Per-tier retry budget.

    Returns:
        The rendered page content.

    Raises:
        ValueError: For an invalid ``mode``/``output``, or a ``dismiss_selector``
            with ``mode="static"`` (the static tier cannot click).
        FetchBlocked: If every applicable tier was blocked or failed.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be auto|static|dynamic|stealth, got {mode!r}")
    if output not in VALID_OUTPUTS:
        raise ValueError(f"output must be markdown|html|text|article, got {output!r}")
    if dismiss_selector and mode == "static":
        raise ValueError("dismiss_selector requires a browser mode (dynamic/stealth/auto)")

    request = FetchRequest(
        url=url, wait_ms=wait_ms, dismiss_selector=dismiss_selector, proxy=proxy
    )

    if mode == "auto":
        result = await escalate(
            request,
            max_retries=max_retries,
            chain=build_auto_chain(dismiss_selector),
        )
        return render_by_type(result, output)

    # Single-tier mode: run exactly one strategy.
    runner = build_tier(TIERS[mode], max_retries)
    result = await runner(request)
    if result is None:
        raise FetchBlocked(f"{mode} fetch blocked/failed for {url}")
    return render_by_type(result, output)
