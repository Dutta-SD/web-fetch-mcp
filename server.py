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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import io
import json
import logging
import os
import random
import re
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from markdownify import markdownify as to_md
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from patchright.async_api import Browser, Playwright, async_playwright
import pypdf
import trafilatura

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("web-fetch")

mcp = FastMCP("web-fetch")

# Headful gives materially better evasion than headless; default headful and
# fall back to headless if the launch fails (e.g. a server with no display).
_HEADLESS = os.environ.get("WEBFETCH_HEADLESS", "0") == "1"

# Retry/backoff tuning (seconds). Applied within a tier before escalating.
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 12.0
_RETRY_AFTER_CAP = 30.0  # cap honored Retry-After so a hostile value can't hang us

# A realistic Chrome header set layered on top of curl_cffi's impersonation.
_DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class FetchBlocked(Exception):
    """Raised when every strategy was blocked or failed."""


@dataclass
class FetchResult:
    """Result of a single fetch attempt from any tier.

    Tier 1 (curl_cffi) populates all fields. Browser tiers (Patchright/nodriver)
    only ever yield rendered HTML, so they fill body/status and leave raw=None,
    headers={} — the PDF/JSON/image paths never apply to them.
    """

    body: str
    status: int
    raw: bytes | None = None
    headers: dict = field(default_factory=dict)
    content_type: str = ""


# ---------- block / challenge detection ----------

# Lowercased substrings that appear in anti-bot interstitials. Kept conservative
# to avoid false positives on legitimate pages that merely discuss these topics.
_BLOCK_MARKERS = (
    "pardon our interruption",
    "access denied",
    "you have been blocked",
    "just a moment...",  # Cloudflare interstitial
    "attention required! | cloudflare",
    "cf-browser-verification",
    "cf-challenge-running",
    "/cdn-cgi/challenge-platform",  # Cloudflare Turnstile/JS challenge
    "_cf_chl_opt",
    "challenge-error-text",
    "verify you are human",
    "verifying you are human",
    "px-captcha",  # PerimeterX / HUMAN
    "please enable javascript and cookies to continue",
    "datadome",  # DataDome challenge payload
    "incapsula incident id",  # Imperva
    "request unsuccessful. incapsula",
    "ddos protection by",
    "please wait for verification",  # Reddit / shreddit verification gate
    "checking your browser before accessing",  # classic anti-bot interstitial
    "checking if the site connection is secure",  # Cloudflare "Just a moment" body
)


def _is_blocked(html: str, status: int) -> bool:
    """True if the response is an anti-bot block/challenge rather than content.

    Catches both hard blocks (403/429) and soft blocks (Akamai et al. return
    HTTP 200 with a block body to fool naive scrapers).
    """
    if status in (401, 403, 429) or status == 503:
        return True
    if not html:
        return False
    head = html[:30_000].lower()
    return any(marker in head for marker in _BLOCK_MARKERS)


# ---------- SPA detection ----------

_SPA_SHELL_PATTERNS = [
    re.compile(r'<div id="root">\s*</div>', re.IGNORECASE),
    re.compile(r'<div id="app">\s*</div>', re.IGNORECASE),
    re.compile(r'<div id="__next">\s*</div>', re.IGNORECASE),
    re.compile(r'<div id="__nuxt">\s*</div>', re.IGNORECASE),
]


def _looks_like_spa_shell(html: str) -> bool:
    """Empty mount-point div, OR very little visible text + many scripts."""
    if any(p.search(html) for p in _SPA_SHELL_PATTERNS):
        return True
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(strip=True)
    scripts = len(soup.find_all("script"))
    return len(text) < 500 and scripts > 3


# ---------- output formatting ----------


def _to_output(html: str, fmt: str) -> str:
    if fmt == "html":
        return html
    if fmt == "text":
        try:
            return BeautifulSoup(html, "lxml").get_text("\n", strip=True)
        except Exception:
            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    if fmt == "article":
        extracted = trafilatura.extract(html, output_format="markdown")
        if extracted and extracted.strip():
            return extracted.strip()
        # not an article (homepage/listing/etc.) -> fall back to full markdown
        return to_md(html, heading_style="ATX")
    return to_md(html, heading_style="ATX")


# Known image magic-byte prefixes for content sniffing.
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"BM", b"\x00\x00\x01\x00")


def _detect_content_type(result: FetchResult) -> str:
    """Classify a fetch result as html | json | pdf | image (header, then sniff)."""
    ct = (result.content_type or "").lower()
    if "application/pdf" in ct:
        return "pdf"
    if "json" in ct:
        return "json"
    if ct.startswith("image/"):
        return "image"
    # An explicit HTML/XHTML header is definitive — don't sniff a body that may
    # legitimately start with '{' (e.g. inline JSON-LD) into being JSON.
    if "html" in ct:
        return "html"
    # sniff body/raw when header is missing or generic (octet-stream, text/plain)
    raw = result.raw
    if raw:
        if raw.startswith(b"%PDF-"):
            return "pdf"
        if any(raw.startswith(m) for m in _IMAGE_MAGIC):
            return "image"
    stripped = result.body.lstrip()
    if stripped[:1] in ("{", "["):
        return "json"
    return "html"


def _render_by_type(result: FetchResult, output: str) -> str:
    """Render a FetchResult per its detected content type.

    JSON -> pretty-printed; PDF -> extracted text; image -> a 'use screenshot'
    note; html -> the normal _to_output path (incl. article mode).
    """
    kind = _detect_content_type(result)
    if kind == "json":
        try:
            return json.dumps(json.loads(result.body), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            return result.body
    if kind == "pdf":
        try:
            reader = pypdf.PdfReader(io.BytesIO(result.raw or b""))
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:  # noqa: BLE001 — corrupt/empty PDF is a real failure
            raise FetchBlocked(
                f"could not extract text from PDF ({type(e).__name__})"
            ) from e
        return text.strip()
    if kind == "image":
        ct = result.content_type or "image"
        return f"[{ct} — use the screenshot tool to view this URL]"
    return _to_output(result.body, output)


# ---------- proxy parsing ----------


def _proxy_for_curl(proxy: Optional[str]) -> Optional[dict]:
    """curl_cffi wants {'http': ..., 'https': ...}."""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _proxy_for_playwright(proxy: Optional[str]) -> Optional[dict]:
    """Playwright wants {'server', 'username'?, 'password'?} with creds split out."""
    if not proxy:
        return None
    p = urlparse(proxy)
    server = f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")
    out: dict = {"server": server}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


# ---------- backoff ----------


def _retry_after_delay(headers: dict) -> Optional[float]:
    """Parse a Retry-After header into a capped, non-negative wait in seconds.

    Accepts integer seconds or an HTTP-date. Returns None when the header is
    absent or unparseable. Capped at _RETRY_AFTER_CAP so a hostile/huge value
    cannot stall the tool.
    """
    raw = headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return min(float(raw), _RETRY_AFTER_CAP)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(delta, 0.0), _RETRY_AFTER_CAP)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    raw = min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt))
    return random.uniform(0, raw)


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


def _normalize_selectors(sel) -> list[str]:
    """Normalize a dismiss/expand selector arg to a list of selector strings."""
    if sel is None:
        return []
    if isinstance(sel, str):
        return [sel]
    return list(sel)


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


