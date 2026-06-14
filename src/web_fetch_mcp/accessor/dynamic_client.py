"""Tier 2: dynamic render via Patchright, plus the screenshot capture.

Both reuse the shared :data:`browser_manager` and its managed-context helper, so
context creation and teardown live in exactly one place.
"""

from __future__ import annotations

from web_fetch_mcp.accessor.browser import browser_manager, open_page
from web_fetch_mcp.core.config import DEFAULT_TIMEOUT_MS
from web_fetch_mcp.core.models import FetchResult


async def fetch_dynamic(
    url: str,
    wait_ms: int = 2000,
    dismiss_selector: str | list[str] | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    proxy: str | None = None,
) -> FetchResult:
    """Render a URL in real headful Chrome and return its HTML.

    Args:
        url: The URL to render.
        wait_ms: Extra settle time (ms) after load.
        dismiss_selector: Optional overlay selector(s) to click after load.
        timeout_ms: Navigation timeout in milliseconds.
        proxy: Optional proxy URL for this context.

    Returns:
        A :class:`FetchResult` with the rendered ``body`` and HTTP ``status``
        (``raw``/``headers`` are unset — browser tiers only yield HTML).
    """
    async with browser_manager.new_managed_context(proxy=proxy) as ctx:
        page, status = await open_page(ctx, url, wait_ms, dismiss_selector, timeout_ms)
        return FetchResult(body=await page.content(), status=status)


async def capture_screenshot(
    url: str,
    full_page: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    wait_ms: int = 2000,
    dismiss_selector: str | list[str] | None = None,
    proxy: str | None = None,
) -> bytes:
    """Render a URL in real Chrome and return a PNG screenshot as bytes.

    Args:
        url: The URL to capture.
        full_page: Capture the full scrollable page when ``True``, else just the
            viewport.
        viewport_width: Viewport width in pixels.
        viewport_height: Viewport height in pixels.
        wait_ms: Extra settle time (ms) after load before capturing.
        dismiss_selector: Optional overlay selector(s) to click before capturing.
        proxy: Optional proxy URL for this context.

    Returns:
        The PNG image as raw bytes.
    """
    viewport = {"width": viewport_width, "height": viewport_height}
    async with browser_manager.new_managed_context(proxy=proxy, viewport=viewport) as ctx:
        page, _status = await open_page(ctx, url, wait_ms, dismiss_selector)
        return await page.screenshot(full_page=full_page, type="png")
