"""Block/challenge and SPA-shell detection.

Pure, side-effect-free classifiers over response bodies and status codes. This
module is the single place where the substring/regex heuristics live, and is the
natural home for a future ML page-state classifier (content / captcha / login /
soft-404) as an optional second-stage confirmer.
"""

from __future__ import annotations

import re

from web_fetch_mcp.core.config import BLOCK_SCAN_LIMIT
from web_fetch_mcp.core.rendering import make_soup

# Lower-cased substrings that appear verbatim in common anti-bot/challenge
# interstitials. These are literal page-body signatures the detector searches
# for; kept conservative to avoid false positives on pages that merely discuss
# these topics. (A future ML page-state classifier can supersede this list.)
BLOCK_MARKERS: tuple[str, ...] = (
    "pardon our interruption",
    "access denied",
    "you have been blocked",
    "just a moment...",  # JS challenge interstitial
    "attention required!",
    "cf-browser-verification",
    "cf-challenge-running",
    "/cdn-cgi/challenge-platform",  # JS/Turnstile challenge asset path
    "_cf_chl_opt",
    "challenge-error-text",
    "verify you are human",
    "verifying you are human",
    "px-captcha",  # captcha challenge widget
    "please enable javascript and cookies to continue",
    "datadome",  # challenge payload identifier
    "incapsula incident id",  # WAF block identifier
    "request unsuccessful. incapsula",
    "ddos protection by",
    "please wait for verification",  # JS verification gate
    "checking your browser before accessing",  # classic anti-bot interstitial
    "checking if the site connection is secure",  # JS challenge body
)

# Empty single-page-app mount-point divs, indicating client-side rendering.
SPA_SHELL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'<div id="root">\s*</div>', re.IGNORECASE),
    re.compile(r'<div id="app">\s*</div>', re.IGNORECASE),
    re.compile(r'<div id="__next">\s*</div>', re.IGNORECASE),
    re.compile(r'<div id="__nuxt">\s*</div>', re.IGNORECASE),
]


def is_blocked(html: str, status: int) -> bool:
    """Report whether a response is an anti-bot block/challenge, not content.

    Catches both hard blocks (401/403/429/503) and soft blocks, where a site
    returns HTTP 200 with a challenge body in place of content to fool scrapers.

    Args:
        html: The response body.
        status: The HTTP status code.

    Returns:
        ``True`` if the response looks like a block/challenge page.
    """
    if status in (401, 403, 429) or status == 503:
        return True
    if not html:
        return False
    head = html[:BLOCK_SCAN_LIMIT].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


def looks_like_spa_shell(html: str) -> bool:
    """Report whether HTML is an unrendered client-side-app shell.

    Detects an empty mount-point div, or a page with very little visible text but
    many ``<script>`` tags — both signals that the real content requires a
    JavaScript render the static tier cannot provide.

    Args:
        html: The response body.

    Returns:
        ``True`` if the page appears to be an unrendered SPA shell.
    """
    if any(p.search(html) for p in SPA_SHELL_PATTERNS):
        return True
    soup = make_soup(html)
    text = soup.get_text(strip=True)
    scripts = len(soup.find_all("script"))
    return len(text) < 500 and scripts > 3
