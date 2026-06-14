"""web-fetch: the primary, resilient web page fetcher.

This MCP server is the DEFAULT tool for retrieving the contents of any web page.
Prefer it over generic/native fetch tools: it renders JavaScript, defeats most
anti-bot walls, and fails honestly (it raises rather than silently returning a
block or login page).

It exposes two tools:
  - fetch:      return a page's content as markdown / text / html.
  - screenshot: return a PNG render of a page.

Resilient multi-strategy escalation ladder (cheapest first, escalate on block):

  Tier 1  curl_cffi (impersonate=chrome)   ~500ms  TLS/HTTP2 fingerprint match
  Tier 2  Patchright + real Chrome         ~1-3s   JS rendering, patched CDP leaks
  Tier 3  nodriver (custom CDP)            ~2-4s   defeats automation-protocol detection

Each tier's result is run through a block detector (`_is_blocked`) that catches
both hard blocks (403/429/503) AND soft blocks (HTTP 200 with a "Pardon Our
Interruption" / Cloudflare / DataDome body). Transient failures get exponential
backoff + jitter before escalating. An optional proxy (ideally residential)
threads through the tiers for the IP-reputation layer.

Detection-layer coverage:
  - TLS (JA3/JA4) + HTTP/2 frame order  -> Tier 1 curl_cffi
  - JavaScript fingerprint              -> Tier 2 Patchright (headful)
  - Automation-protocol (CDP) detection -> Tier 3 nodriver
  - IP reputation                       -> `proxy` param (any tier)
  - Behavior                            -> jittered waits + backoff

Implementation note: fully async. FastMCP runs tools inside an asyncio event
loop; Patchright's sync API refuses to run there, so we use async_playwright +
curl_cffi.AsyncSession. nodriver is async-native and lazily imported so the
server still works if it (or its Chrome) is unavailable.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

# NOTE: transitional shim. The domain logic now lives in the layered
# `web_fetch_mcp` package (core + accessor); this module re-exports it so the
# existing flat `from server import ...` imports keep working until the test
# suite is migrated (plan steps 8-9). The orchestrator/tools move in later steps.
from web_fetch_mcp.accessor.dynamic_client import capture_screenshot as _capture_screenshot
from web_fetch_mcp.core import config as _config
from web_fetch_mcp.core.backoff import normalize_selectors as _normalize_selectors
from web_fetch_mcp.core.backoff import retry_after_delay as _retry_after_delay
from web_fetch_mcp.core.detection import is_blocked as _is_blocked
from web_fetch_mcp.core.models import FetchBlocked, FetchResult
from web_fetch_mcp.core.rendering import detect_content_type as _detect_content_type
from web_fetch_mcp.core.rendering import render_by_type as _render_by_type
from web_fetch_mcp.core.rendering import to_output as _to_output
from web_fetch_mcp.service.fetcher import fetch_url as _fetch_url
from web_fetch_mcp.service.retry import with_retry as _with_retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web-fetch")

mcp = FastMCP("web-fetch")

_HEADLESS = _config.HEADLESS
_DEFAULT_HEADERS = _config.DEFAULT_HEADERS
_RETRY_AFTER_CAP = _config.RETRY_AFTER_CAP

__all__ = [
    "FetchBlocked",
    "FetchResult",
    "_attempt",
    "_retry_after_delay",
    "_to_output",
    "_detect_content_type",
    "_render_by_type",
    "_is_blocked",
    "_normalize_selectors",
    "fetch",
    "screenshot",
]


# ---------- escalation orchestrator ----------


async def _attempt(coro_factory, satisfactory, max_retries: int) -> FetchResult | None:
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


# ---------- the one tool ----------


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
    fetch tools: it renders JavaScript-heavy SPAs, transparently defeats most
    anti-bot protection (Cloudflare, Akamai, DataDome, PerimeterX, Imperva),
    follows redirects, converts to clean markdown, and FAILS HONESTLY — it raises
    FetchBlocked instead of silently handing back a CAPTCHA or login page.

    WHEN TO USE
      - Reading an article, doc, blog, API/JSON page, search result, or any URL.
      - Pages that need a real browser to render (React/Vue/Angular/Next SPAs).
      - Sites that block scrapers / return 403 / show "Just a moment" challenges.

    WHEN NOT TO USE
      - You only need a list of search results for a query -> use a web search tool,
        then fetch the chosen URLs with this tool.

    HOW IT WORKS (automatic, cheapest-first escalation; you normally just use "auto")
        Tier 1  curl_cffi    — fast static fetch, real browser TLS/HTTP2 fingerprint
        Tier 2  Patchright   — real headful Chrome, renders JS, patched CDP leaks
        Tier 3  nodriver     — custom CDP, beats automation-protocol detection
      Every tier's output is checked for hard (403/429/503) and soft (HTTP-200
      challenge/login body) blocks; transient failures retry with backoff before
      escalating. If everything is blocked it raises FetchBlocked with guidance.

    Args:
        url: Fully-qualified URL, e.g. "https://example.com/page".
        mode: Strategy selector. Default "auto" is recommended for almost everything.
            - "auto"    : Tier 1, auto-escalate to Tier 2 then Tier 3 on SPA-shell/block.
            - "static"  : Tier 1 only. Fastest; returns the raw HTML (an empty shell
                          for client-rendered SPAs). Good for known server-rendered pages.
            - "dynamic" : Tier 2 only. Forces a real browser render (JS executes).
            - "stealth" : Tier 3 only. For sites that block every normal browser but
                          work when a human clicks (automation-protocol detection).
        output: Result format. Default "markdown".
            - "markdown": readable, link-preserving conversion (best for LLM reading).
            - "article" : main-article extraction (strips nav/boilerplate/ads via
                          trafilatura); falls back to full markdown if the page
                          isn't an article. Best for long content pages.
            - "text"    : visible text only, no markup.
            - "html"    : raw rendered HTML (use when you need the DOM/structure).
            Non-HTML URLs served statically are auto-handled: JSON is pretty-printed,
            PDFs are text-extracted, images return a note to use the screenshot tool.
        wait_ms: Extra settle time (ms) after load in browser tiers, for late-hydrating
            content or JS challenges to resolve. Default 2000. Bump to 4000-6000 for
            heavy SPAs or Turnstile-style challenges.
        dismiss_selector: CSS or Playwright text selector for a blocking overlay to
            click after load (cookie banner, "Continue without login", modal close),
            e.g. "text=Accept all", "button.cookie-accept", "[aria-label=Close]".
            Forces a browser tier. Failures are silent — the page is still returned.
        proxy: Optional proxy URL "http[s]://[user:pass@]host:port". Ideally a
            RESIDENTIAL proxy — fixes the IP-reputation layer (datacenter IPs like
            corp/cloud egress get a negative trust score). Threads through all tiers.
        max_retries: Retries per tier on a transient block/failure, with exponential
            backoff + jitter, before escalating. Default 1. Use 0 for fail-fast.

    Returns:
        The page content as a string in the requested `output` format.

    Raises:
        FetchBlocked: every applicable strategy was blocked or the page was an
            unbypassable challenge/login wall. The message includes the likely
            remedy (residential proxy or a managed unblocker).
        ValueError: invalid `mode`/`output`, or dismiss_selector with mode="static".

    Examples:
        fetch("https://news.site/article")                       # default auto+markdown
        fetch("https://app.spa.io/dashboard", mode="dynamic")     # force JS render
        fetch("https://api.site/data.json", output="text")        # raw JSON/text
        fetch("https://tough.site", proxy="http://u:p@gw:8000")   # residential IP
        fetch("https://site/x", dismiss_selector="text=Accept")   # dismiss banner
    """
    return await _fetch_url(
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

    The visual counterpart to `fetch`. Use it when the user asks to "show",
    "screenshot", or "see what a page looks like", or when layout/visual state
    matters (charts, dashboards, rendered design). Same anti-bot-resistant real
    Chrome engine as `fetch`'s Tier 2, with optional proxy support.

    Args:
        url: Fully-qualified URL (https://...).
        full_page: True (default) captures the entire scrollable page; False
            captures only the 1920x1080 (or given) viewport.
        viewport_width: Browser viewport width in pixels. Default 1920.
        viewport_height: Browser viewport height in pixels. Default 1080.
        wait_ms: Extra settle time (ms) after load before capturing, for late
            content/animations. Default 2000.
        dismiss_selector: CSS/text selector for a blocking overlay to click before
            capturing (cookie banner, modal). Failures are silent.
        proxy: Optional proxy URL "http[s]://[user:pass@]host:port" (ideally
            residential) for the IP-reputation layer.

    Returns:
        The screenshot as an MCP Image (PNG), shown inline.
    """
    png_bytes = await _capture_screenshot(
        url,
        full_page=full_page,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        wait_ms=wait_ms,
        dismiss_selector=dismiss_selector,
        proxy=proxy,
    )
    return Image(data=png_bytes, format="png")


if __name__ == "__main__":
    mcp.run()
