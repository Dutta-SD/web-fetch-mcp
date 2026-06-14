"""Tier 3: stealth render via nodriver (custom CDP, beats automation detection)."""

from __future__ import annotations

import asyncio
import logging

from web_fetch_mcp.core.config import HEADLESS
from web_fetch_mcp.core.models import FetchResult
from web_fetch_mcp.core.proxy import proxy_for_nodriver

log = logging.getLogger("web-fetch")


async def fetch_nodriver(
    url: str,
    wait_ms: int = 2500,
    proxy: str | None = None,
    timeout_ms: int = 30_000,
) -> FetchResult:
    """Drive Chrome over nodriver's custom CDP implementation.

    nodriver is imported lazily so the server still runs when it (or its Chrome)
    is unavailable. nodriver does not expose an HTTP status, so the result's
    ``status`` is ``0`` and block detection relies on body markers.

    Args:
        url: The URL to render.
        wait_ms: Settle time (ms) after load for JS challenges to resolve.
        proxy: Optional proxy URL, passed as a Chrome ``--proxy-server`` arg.
        timeout_ms: Unused placeholder kept for signature parity with other tiers.

    Returns:
        A :class:`FetchResult` with the rendered ``body`` and ``status=0``.
    """
    import nodriver as uc  # lazy

    browser_args = []
    proxy_arg = proxy_for_nodriver(proxy)
    if proxy_arg:
        browser_args.append(f"--proxy-server={proxy_arg}")

    browser = None
    try:
        # nodriver only waits ~2.5s for the CDP endpoint. On hosts where Chrome is
        # slow to start (e.g. an x86_64 Chrome under Rosetta on Apple Silicon) the
        # first launch can lose that race, so retry — Rosetta/page cache warms
        # after the first attempt and subsequent starts connect quickly.
        async def _nd_start(headless: bool):
            last_err: Exception | None = None
            for i in range(3):
                try:
                    return await uc.start(
                        headless=headless,
                        sandbox=False,
                        browser_args=browser_args or None,
                    )
                except Exception as e:  # noqa: BLE001 — retry the CDP race
                    last_err = e
                    log.warning(
                        "nodriver start (headless=%s) attempt %d failed: %s",
                        headless,
                        i,
                        e,
                    )
                    await asyncio.sleep(1.0)
            raise last_err  # type: ignore[misc]

        try:
            browser = await _nd_start(HEADLESS)
        except Exception as e:  # noqa: BLE001 — headful needs a display; fall back
            log.warning("nodriver headful unavailable (%s); falling back to headless", e)
            browser = await _nd_start(True)

        page = await browser.get(url)
        # Let the page settle / any JS challenge resolve.
        await page.sleep(max(wait_ms, 1000) / 1000)
        try:
            html = await page.get_content()
        except Exception:  # noqa: BLE001 — fall back to evaluating the DOM
            html = await page.evaluate(
                "document.documentElement.outerHTML", return_by_value=True
            )
        return FetchResult(body=(html or ""), status=0)
    finally:
        if browser is not None:
            try:
                browser.stop()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
