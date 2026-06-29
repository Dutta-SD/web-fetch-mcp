"""The ``fetch_url`` facade: the framework-free entry point for fetching.

Validates arguments, checks the domain-level circuit breaker, dispatches to a
single tier or the auto escalation chain, records success/failure with the
circuit, and renders every result through one content-type-aware path. The
controller's ``fetch`` tool is a thin wrapper over this.
"""

from __future__ import annotations

from enum import StrEnum

from web_fetch_mcp.core.models import FetchBlocked, FetchMode, OutputFormat
from web_fetch_mcp.core.rendering import render_by_type
from web_fetch_mcp.service.circuit import domain_circuits
from web_fetch_mcp.service.escalation import build_auto_chain, escalate
from web_fetch_mcp.service.request import FetchRequest
from web_fetch_mcp.service.strategies import TIERS, build_tier


def _parse_enum(enum_cls: type[StrEnum], value: str, param_name: str) -> StrEnum:
    """Validate and convert a string to an enum member, or raise ValueError."""
    try:
        return enum_cls(value)
    except ValueError:
        valid = ", ".join(e.value for e in enum_cls)
        raise ValueError(f"{param_name} must be one of [{valid}], got {value!r}") from None


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

    Before attempting any fetch, the domain-level circuit breaker is consulted:
    if the domain has too many recent consecutive failures (captchas, blocks),
    the request is rejected immediately with an actionable ``FetchBlocked``
    (circuit open) — no browser is launched, no time is wasted.

    In ``auto`` mode the request escalates cheapest-first across the tiers; the
    single-tier modes run exactly one. Every result — from any tier — is rendered
    through :func:`render_by_type`, so JSON/PDF/image handling applies uniformly
    (browser tiers always classify as HTML and fall through to markdown/text).

    On success the circuit records a success (closing it if it was half-open);
    on failure the circuit records a failure (potentially opening it for future
    requests to this domain).

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
        FetchBlocked: If the circuit is open for this domain, or if every
            applicable tier was blocked or failed.
    """
    resolved_mode = _parse_enum(FetchMode, mode, "mode")
    resolved_output = _parse_enum(OutputFormat, output, "output")

    if dismiss_selector and resolved_mode == FetchMode.STATIC:
        raise ValueError(
            "dismiss_selector requires a browser mode (dynamic/stealth/auto)"
        )

    domain_circuits.check(url)

    request = FetchRequest(
        url=url, wait_ms=wait_ms, dismiss_selector=dismiss_selector, proxy=proxy
    )

    if resolved_mode == FetchMode.AUTO:
        try:
            result = await escalate(
                request,
                max_retries=max_retries,
                chain=build_auto_chain(dismiss_selector),
            )
        except FetchBlocked as exc:
            domain_circuits.record_failure(url, reason=str(exc))
            raise
    else:
        runner = build_tier(TIERS[resolved_mode.value], max_retries)
        result = await runner(request)
        if result is None:
            domain_circuits.record_failure(url, reason="all tiers exhausted")
            raise FetchBlocked(f"{resolved_mode.value} fetch blocked/failed for {url}")

    domain_circuits.record_success(url)
    return render_by_type(result, resolved_output.value)
