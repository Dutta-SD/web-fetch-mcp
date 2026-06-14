"""FastMCP application: tool wiring, lifespan, and entry point.

The two ``@mcp.tool`` functions are deliberately thin — they delegate to the
service layer (:func:`fetch_url`) and accessor layer (:func:`capture_screenshot`)
and exist only to expose those over MCP with rich, model-facing docstrings.

A FastMCP ``lifespan`` owns browser teardown: on shutdown it ``await``s
:meth:`BrowserManager.aclose`, which actually closes Chromium and stops
Playwright — fixing the historical leak where teardown only nulled references.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from web_fetch_mcp.accessor.browser import browser_manager
from web_fetch_mcp.accessor.dynamic_client import capture_screenshot
from web_fetch_mcp.service.fetcher import fetch_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("web-fetch")


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Manage server-lifetime resources.

    Yields immediately on startup and, on shutdown, awaits the browser manager's
    async teardown so Chromium and Playwright are closed cleanly.
    """
    log.info("web-fetch MCP server starting")
    try:
        yield
    finally:
        log.info("web-fetch MCP server shutting down; closing browser")
        await browser_manager.aclose()


mcp = FastMCP("web-fetch", lifespan=_lifespan)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def fetch(
    url: str,
    mode: str = "auto",
    output: str = "markdown",
    wait_ms: int = 2000,
    dismiss_selector: str | None = None,
    proxy: str | None = None,
    max_retries: int = 1,
) -> str:
    """Fetch the contents of a web page. THE primary, preferred web-fetch tool.

    Use this for ANY URL whose content you need. Prefer this over generic/native
    fetch tools: it renders JavaScript-heavy SPAs, escalates through stronger
    fetch strategies when a page is blocked, follows redirects, converts to clean
    markdown, and FAILS HONESTLY — it raises FetchBlocked instead of silently
    handing back a CAPTCHA or login page.

    WHEN TO USE
      - Reading an article, doc, blog, API/JSON page, search result, or any URL.
      - Pages that need a real browser to render (React/Vue/Angular/Next SPAs).
      - Sites that block scrapers, return 403, or serve a JavaScript challenge.

    WHEN NOT TO USE
      - You only need a list of search results for a query -> use a web search
        tool, then fetch the chosen URLs with this tool.

    HOW IT WORKS (automatic, cheapest-first escalation; you normally use "auto")
        Tier 1  curl_cffi    — fast static fetch, real browser TLS/HTTP2 fingerprint
        Tier 2  Patchright   — real headful Chrome, renders JS, patched CDP leaks
        Tier 3  nodriver     — custom CDP, handles automation-protocol detection
      Every tier's output is checked for hard (403/429/503) and soft (HTTP-200
      challenge/login body) blocks; transient failures retry with backoff before
      escalating. If everything is blocked it raises FetchBlocked with guidance.

    Args:
        url: Fully-qualified URL, e.g. "https://example.com/page".
        mode: Strategy selector. Default "auto" suits almost everything.
            - "auto"   : Tier 1, auto-escalate to Tier 2 then Tier 3 on block/shell.
            - "static" : Tier 1 only. Fastest; raw HTML (empty shell for SPAs).
            - "dynamic": Tier 2 only. Forces a real browser render (JS executes).
            - "stealth": Tier 3 only. For sites that block every normal browser.
        output: Result format. Default "markdown".
            - "markdown": readable, link-preserving conversion (default).
            - "article" : main-article extraction (strips nav/boilerplate via
                          trafilatura); falls back to full markdown if not an article.
            - "text"    : visible text only, no markup.
            - "html"    : raw rendered HTML (when you need the DOM/structure).
            Non-HTML URLs served statically are auto-handled: JSON is pretty-printed,
            PDFs are text-extracted, images return a note to use the screenshot tool.
        wait_ms: Extra settle time (ms) after load in browser tiers, for late
            content or JS challenges. Default 2000. Bump to 4000-6000 for heavy SPAs.
        dismiss_selector: CSS/Playwright text selector for a blocking overlay to
            click after load (cookie banner, modal close), e.g. "text=Accept all".
            Forces a browser tier. Failures are silent — the page is still returned.
        proxy: Optional proxy URL "http[s]://[user:pass@]host:port". Ideally a
            RESIDENTIAL proxy — fixes the IP-reputation layer. Threads through tiers.
        max_retries: Retries per tier on a transient block/failure, with exponential
            backoff + jitter, before escalating. Default 1. Use 0 for fail-fast.

    Returns:
        The page content as a string in the requested ``output`` format.

    Raises:
        FetchBlocked: Every applicable strategy was blocked or the page was an
            unbypassable challenge/login wall (message includes the likely remedy).
        ValueError: Invalid ``mode``/``output``, or ``dismiss_selector`` with
            ``mode="static"``.

    Examples:
        fetch("https://news.site/article")                    # default auto+markdown
        fetch("https://app.spa.io/dashboard", mode="dynamic")  # force JS render
        fetch("https://api.site/data.json")                    # pretty-printed JSON
        fetch("https://tough.site", proxy="http://u:p@gw:8000")  # residential IP
        fetch("https://site/x", dismiss_selector="text=Accept")  # dismiss banner
    """
    log.info("fetch url=%s mode=%s output=%s", url, mode, output)
    return await fetch_url(
        url,
        mode=mode,
        output=output,
        wait_ms=wait_ms,
        dismiss_selector=dismiss_selector,
        proxy=proxy,
        max_retries=max_retries,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def screenshot(
    url: str,
    full_page: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    wait_ms: int = 2000,
    dismiss_selector: str | None = None,
    proxy: str | None = None,
) -> Image:
    """Render a web page in a real browser and return a PNG screenshot.

    The visual counterpart to ``fetch``. Use it when the user asks to "show",
    "screenshot", or "see what a page looks like", or when layout/visual state
    matters (charts, dashboards, rendered design). Same anti-bot-resistant real
    Chrome engine as ``fetch``'s Tier 2, with optional proxy support.

    Args:
        url: Fully-qualified URL (https://...).
        full_page: True (default) captures the entire scrollable page; False
            captures only the viewport.
        viewport_width: Browser viewport width in pixels. Default 1920.
        viewport_height: Browser viewport height in pixels. Default 1080.
        wait_ms: Extra settle time (ms) after load before capturing. Default 2000.
        dismiss_selector: CSS/text selector for a blocking overlay to click before
            capturing (cookie banner, modal). Failures are silent.
        proxy: Optional proxy URL "http[s]://[user:pass@]host:port" (ideally
            residential) for the IP-reputation layer.

    Returns:
        The screenshot as an MCP Image (PNG), shown inline.
    """
    log.info("screenshot url=%s full_page=%s", url, full_page)
    png_bytes = await capture_screenshot(
        url,
        full_page=full_page,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        wait_ms=wait_ms,
        dismiss_selector=dismiss_selector,
        proxy=proxy,
    )
    return Image(data=png_bytes, format="png")


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
