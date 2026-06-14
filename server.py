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


