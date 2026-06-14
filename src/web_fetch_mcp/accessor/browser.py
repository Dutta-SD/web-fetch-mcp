"""Patchright browser ownership and page navigation.

``BrowserManager`` is the single owner of the shared Playwright/Browser, replacing
the former module-level globals. It lazily launches one Chromium, reuses it across
calls, hands out managed contexts, and — unlike the old ``atexit`` shutdown that
merely nulled the globals — actually ``await``-closes the browser on teardown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from patchright.async_api import Browser, Playwright, async_playwright

from web_fetch_mcp.core.backoff import normalize_selectors
from web_fetch_mcp.core.config import (
    BROWSER_LOCALE,
    BROWSER_TIMEZONE,
    BROWSER_VIEWPORT,
    DEFAULT_TIMEOUT_MS,
    HEADLESS,
)
from web_fetch_mcp.core.proxy import proxy_for_playwright

log = logging.getLogger("web-fetch")

# Chromium navigation errors that mean the host is unreachable, not slow. On
# these, retrying with a softer wait would only load the browser's error page,
# so the tier must fail (and escalate/raise) instead of returning that chrome.
_FATAL_NAV_ERRORS = (
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_SSL_PROTOCOL_ERROR",
)


class BrowserManager:
    """Lazily launches and reuses a single Patchright Chromium.

    A module-level singleton instance (:data:`browser_manager`) is shared by the
    dynamic fetch tier and the screenshot tool so they reuse one browser process.
    """

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def get_browser(self) -> Browser:
        """Return the shared browser, launching it on first use.

        Launch order: real Chrome headful, then real Chrome headless, then the
        bundled Chromium. Guarded by a lock so concurrent callers share one
        launch.

        Returns:
            A connected Patchright :class:`Browser`.

        Raises:
            RuntimeError: If no Chromium variant could be launched.
        """
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                attempts = (
                    [] if HEADLESS else [{"headless": False, "channel": "chrome"}]
                ) + [
                    {"headless": True, "channel": "chrome"},
                    {"headless": True},
                ]
                last_err: Exception | None = None
                for kw in attempts:
                    try:
                        log.info("launching patchright chromium %s", kw)
                        self._browser = await self._pw.chromium.launch(**kw)
                        break
                    except Exception as e:  # noqa: BLE001 — try the next variant
                        last_err = e
                        log.warning("launch %s failed: %s", kw, e)
                if self._browser is None:
                    raise RuntimeError(f"could not launch any chromium: {last_err}")
            return self._browser

    @asynccontextmanager
    async def new_managed_context(
        self, proxy: str | None = None, viewport: dict | None = None
    ) -> AsyncIterator[object]:
        """Yield a fresh browser context, always closed on exit.

        Args:
            proxy: Optional proxy URL for this context.
            viewport: Viewport size; defaults to :data:`BROWSER_VIEWPORT`.

        Yields:
            A Playwright ``BrowserContext``.
        """
        browser = await self.get_browser()
        ctx = await browser.new_context(
            viewport=viewport or BROWSER_VIEWPORT,
            locale=BROWSER_LOCALE,
            timezone_id=BROWSER_TIMEZONE,
            proxy=proxy_for_playwright(proxy),
        )
        try:
            yield ctx
        finally:
            await ctx.close()

    async def aclose(self) -> None:
        """Close the browser and stop Playwright, awaiting both.

        Fixes the historical leak where shutdown only nulled references without
        awaiting ``browser.close()`` / ``playwright.stop()``.
        """
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            self._pw = None


# Shared singleton: the dynamic tier and screenshot tool reuse one browser.
browser_manager = BrowserManager()


async def open_page(
    ctx,
    url: str,
    wait_ms: int,
    dismiss_selector: str | list[str] | None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> tuple[object, int]:
    """Navigate a page, settle, and optionally dismiss a blocking overlay.

    Args:
        ctx: The Playwright browser context to open the page in.
        url: The URL to navigate to.
        wait_ms: Extra settle time (ms) after load.
        dismiss_selector: Selector(s) for an overlay to click; the first that
            matches wins. Failures are logged and ignored.
        timeout_ms: Navigation timeout in milliseconds.

    Returns:
        A ``(page, status)`` tuple. ``status`` is ``0`` if no response object was
        available.
    """
    page = await ctx.new_page()
    status = 0
    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        status = resp.status if resp else 0
    except Exception as e:  # noqa: BLE001 — distinguish "slow" from "unreachable"
        # A hard connection/DNS failure is fatal: retrying would just load the
        # browser's own error page (e.g. "this site can't be reached"), which we
        # must NOT return as content. Only a settle/timeout warrants a softer
        # retry with domcontentloaded.
        if any(tok in str(e) for tok in _FATAL_NAV_ERRORS):
            raise
        log.warning("networkidle failed (%s); retrying with domcontentloaded", e)
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        status = resp.status if resp else 0

    if wait_ms > 0:
        await page.wait_for_timeout(wait_ms)

    for sel in normalize_selectors(dismiss_selector):
        try:
            await page.click(sel, timeout=3000)
            log.info("clicked element matching %r", sel)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001 — best-effort settle
                pass
            await page.wait_for_timeout(1500)
            break  # first match wins
        except Exception as e:  # noqa: BLE001 — selector optional; keep going
            log.info("selector %r not clickable (%s); continuing", sel, e)

    return page, status
