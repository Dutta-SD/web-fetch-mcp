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

import asyncio
import atexit
import logging
import os
from typing import Optional

from curl_cffi.requests import AsyncSession
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from patchright.async_api import Browser, Playwright, async_playwright

# NOTE: transitional shim. The pure domain logic now lives in the layered
# `web_fetch_mcp.core` package; this module re-exports it so the existing flat
# `from server import ...` imports keep working until the test suite is migrated
# (plan steps 8-9). The accessor/orchestrator/tool code below is moved in later
# steps. Underscore-prefixed aliases preserve the historical names.
from web_fetch_mcp.core import config as _config
from web_fetch_mcp.core.backoff import backoff_delay as _backoff_delay
from web_fetch_mcp.core.backoff import normalize_selectors as _normalize_selectors
from web_fetch_mcp.core.backoff import retry_after_delay as _retry_after_delay
from web_fetch_mcp.core.detection import is_blocked as _is_blocked
from web_fetch_mcp.core.detection import looks_like_spa_shell as _looks_like_spa_shell
from web_fetch_mcp.core.models import FetchBlocked, FetchResult
from web_fetch_mcp.core.proxy import proxy_for_curl as _proxy_for_curl
from web_fetch_mcp.core.proxy import proxy_for_nodriver as _proxy_for_nodriver
from web_fetch_mcp.core.proxy import proxy_for_playwright as _proxy_for_playwright
from web_fetch_mcp.core.rendering import detect_content_type as _detect_content_type
from web_fetch_mcp.core.rendering import render_by_type as _render_by_type
from web_fetch_mcp.core.rendering import to_output as _to_output

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


# ---------- Tier 1: static (curl_cffi) ----------


async def _fetch_static(
    url: str,
    proxy: Optional[str] = None,
    timeout: int = 25,
) -> FetchResult:
    """Browser-grade TLS fingerprint. Returns a FetchResult. Does not raise on 4xx/5xx."""
    async with AsyncSession() as s:
        r = await s.get(
            url,
            impersonate="chrome",
            headers=_DEFAULT_HEADERS,
            proxies=_proxy_for_curl(proxy),
            timeout=timeout,
            allow_redirects=True,
        )
        headers = {k.lower(): v for k, v in r.headers.items()}
        return FetchResult(
            body=r.text,
            status=r.status_code,
            raw=r.content,
            headers=headers,
            content_type=headers.get("content-type", ""),
        )


# ---------- Tier 2: dynamic (Patchright + reused browser) ----------

_pw: Optional[Playwright] = None
_browser: Optional[Browser] = None
_browser_lock = asyncio.Lock()


async def _get_browser() -> Browser:
    """Lazy-init a single Patchright Chromium, reused across calls.

    Launch order: real Chrome headful -> real Chrome headless -> bundled chromium.
    """
    global _pw, _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            if _pw is None:
                _pw = await async_playwright().start()
            attempts = (
                [] if _HEADLESS else [{"headless": False, "channel": "chrome"}]
            ) + [
                {"headless": True, "channel": "chrome"},
                {"headless": True},
            ]
            last_err: Optional[Exception] = None
            for kw in attempts:
                try:
                    log.info("launching patchright chromium %s", kw)
                    _browser = await _pw.chromium.launch(**kw)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("launch %s failed: %s", kw, e)
            if _browser is None:
                raise RuntimeError(f"could not launch any chromium: {last_err}")
        return _browser


async def _open_page(
    ctx,
    url: str,
    wait_ms: int,
    dismiss_selector: str | list[str] | None,
    timeout_ms: int = 30_000,
) -> tuple[object, int]:
    """Navigate, wait, optionally dismiss a blocker. Returns (page, status)."""
    page = await ctx.new_page()
    status = 0
    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        status = resp.status if resp else 0
    except Exception as e:
        log.warning("networkidle failed (%s); retrying with domcontentloaded", e)
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        status = resp.status if resp else 0

    if wait_ms > 0:
        await page.wait_for_timeout(wait_ms)

    for sel in _normalize_selectors(dismiss_selector):
        try:
            await page.click(sel, timeout=3000)
            log.info("clicked element matching %r", sel)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            break  # first match wins
        except Exception as e:
            log.info("selector %r not clickable (%s); continuing", sel, e)

    return page, status


async def _fetch_dynamic(
    url: str,
    wait_ms: int = 2000,
    dismiss_selector: str | list[str] | None = None,
    timeout_ms: int = 30_000,
    proxy: Optional[str] = None,
) -> FetchResult:
    browser = await _get_browser()
    ctx = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        proxy=_proxy_for_playwright(proxy),
    )
    try:
        page, status = await _open_page(ctx, url, wait_ms, dismiss_selector, timeout_ms)
        return FetchResult(body=await page.content(), status=status)
    finally:
        await ctx.close()


# ---------- Tier 3: nodriver (defeats automation-protocol detection) ----------


async def _fetch_nodriver(
    url: str,
    wait_ms: int = 2500,
    proxy: Optional[str] = None,
    timeout_ms: int = 30_000,
) -> FetchResult:
    """Drive Chrome over a custom CDP impl (not the standard automation interface).

    Lazily imported so the server still runs if nodriver/Chrome is unavailable.
    Status is not exposed by nodriver, so we return 0 and rely on body markers.
    """
    import nodriver as uc  # lazy

    browser_args = []
    proxy_arg = _proxy_for_nodriver(proxy)
    if proxy_arg:
        browser_args.append(f"--proxy-server={proxy_arg}")

    browser = None
    try:
        # nodriver only waits ~2.5s for the CDP endpoint. On hosts where Chrome
        # is slow to start (e.g. an x86_64 Chrome under Rosetta on Apple Silicon)
        # the first launch can lose that race, so retry — Rosetta/page cache warms
        # after the first attempt and subsequent starts connect quickly.
        async def _nd_start(headless: bool):
            last_err: Optional[Exception] = None
            for i in range(3):
                try:
                    return await uc.start(
                        headless=headless,
                        sandbox=False,
                        browser_args=browser_args or None,
                    )
                except Exception as e:  # noqa: BLE001
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
            browser = await _nd_start(_HEADLESS)
        except Exception as e:  # headful needs a display; fall back to headless
            log.warning(
                "nodriver headful unavailable (%s); falling back to headless", e
            )
            browser = await _nd_start(True)

        page = await browser.get(url)
        # let the page settle / any JS challenge resolve
        await page.sleep(max(wait_ms, 1000) / 1000)
        try:
            html = await page.get_content()
        except Exception:
            html = await page.evaluate(
                "document.documentElement.outerHTML", return_by_value=True
            )
        return FetchResult(body=(html or ""), status=0)
    finally:
        if browser is not None:
            try:
                browser.stop()
            except Exception:
                pass


def _shutdown() -> None:
    """Best-effort sync cleanup at interpreter exit."""
    global _pw, _browser
    _browser = None
    _pw = None


atexit.register(_shutdown)


# ---------- escalation orchestrator ----------


async def _attempt(coro_factory, satisfactory, max_retries: int) -> Optional[FetchResult]:
    """Run one strategy with retry+backoff. Returns the FetchResult if satisfactory, else None.

    coro_factory: zero-arg callable returning a fresh awaitable -> FetchResult
    satisfactory: (FetchResult) -> bool  (True means 'good content, stop')
    """
    for attempt in range(max_retries + 1):
        result: Optional[FetchResult] = None
        try:
            result = await coro_factory()
        except Exception as e:  # noqa: BLE001 — network/launch error
            log.info("strategy attempt %d errored: %s", attempt, e)
            if attempt < max_retries:
                await asyncio.sleep(_backoff_delay(attempt))
                continue
            return None
        if satisfactory(result):
            return result
        log.info(
            "strategy attempt %d unsatisfactory (status=%s, blocked=%s)",
            attempt,
            result.status,
            _is_blocked(result.body, result.status),
        )
        if attempt < max_retries:
            ra = _retry_after_delay(result.headers)
            await asyncio.sleep(ra if ra is not None else _backoff_delay(attempt))
    return None


# ---------- the one tool ----------


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def fetch(
    url: str,
    mode: str = "auto",
    output: str = "markdown",
    wait_ms: int = 2000,
    dismiss_selector: Optional[str] = None,
    proxy: Optional[str] = None,
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
    if mode not in {"auto", "static", "dynamic", "stealth"}:
        raise ValueError(f"mode must be auto|static|dynamic|stealth, got {mode!r}")
    if output not in {"markdown", "html", "text", "article"}:
        raise ValueError(f"output must be markdown|html|text|article, got {output!r}")
    if dismiss_selector and mode == "static":
        raise ValueError(
            "dismiss_selector requires a browser mode (dynamic/stealth/auto)"
        )

    not_blocked = lambda r: not _is_blocked(r.body, r.status)  # noqa: E731

    # ----- single-tier modes -----
    if mode == "static":
        result = await _attempt(
            lambda: _fetch_static(url, proxy), not_blocked, max_retries
        )
        if result is None:
            raise FetchBlocked(f"static fetch blocked/failed for {url}")
        return _render_by_type(result, output)

    if mode == "dynamic":
        result = await _attempt(
            lambda: _fetch_dynamic(url, wait_ms, dismiss_selector, proxy=proxy),
            not_blocked,
            max_retries,
        )
        if result is None:
            raise FetchBlocked(f"dynamic fetch blocked/failed for {url}")
        return _to_output(result.body, output)

    if mode == "stealth":
        result = await _attempt(
            lambda: _fetch_nodriver(url, wait_ms, proxy), not_blocked, max_retries
        )
        if result is None:
            raise FetchBlocked(f"stealth (nodriver) fetch blocked/failed for {url}")
        return _to_output(result.body, output)

    # ----- auto: escalate Tier 1 -> Tier 2 -> Tier 3 -----
    # dismiss_selector needs a browser; skip straight to Tier 2.
    if not dismiss_selector:
        static_ok = lambda r: (  # noqa: E731
            not _is_blocked(r.body, r.status) and not _looks_like_spa_shell(r.body)
        )
        result = await _attempt(
            lambda: _fetch_static(url, proxy), static_ok, max_retries
        )
        if result is not None:
            return _render_by_type(result, output)
        log.info("Tier 1 (static) insufficient; escalating to Tier 2 (Patchright)")

    result = await _attempt(
        lambda: _fetch_dynamic(url, wait_ms, dismiss_selector, proxy=proxy),
        not_blocked,
        max_retries,
    )
    if result is not None:
        return _to_output(result.body, output)
    log.info("Tier 2 (Patchright) blocked; escalating to Tier 3 (nodriver)")

    result = await _attempt(
        lambda: _fetch_nodriver(url, wait_ms, proxy), not_blocked, max_retries
    )
    if result is not None:
        return _to_output(result.body, output)

    raise FetchBlocked(
        f"all strategies (static, patchright, nodriver) blocked/failed for {url}. "
        f"Try a residential proxy= or, for CAPTCHA-gated sites, a managed unblocker API."
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def screenshot(
    url: str,
    full_page: bool = True,
    viewport_width: int = 1920,
    viewport_height: int = 1080,
    wait_ms: int = 2000,
    dismiss_selector: Optional[str] = None,
    proxy: Optional[str] = None,
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
    browser = await _get_browser()
    ctx = await browser.new_context(
        viewport={"width": viewport_width, "height": viewport_height},
        locale="en-US",
        timezone_id="America/New_York",
        proxy=_proxy_for_playwright(proxy),
    )
    try:
        page, _status = await _open_page(ctx, url, wait_ms, dismiss_selector)
        png_bytes = await page.screenshot(full_page=full_page, type="png")
        return Image(data=png_bytes, format="png")
    finally:
        await ctx.close()


if __name__ == "__main__":
    mcp.run()
