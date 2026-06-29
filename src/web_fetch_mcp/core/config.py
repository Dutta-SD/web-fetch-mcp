"""Central configuration constants for web-fetch-mcp.

All cross-cutting tunables live here so they are defined in exactly one place
rather than scattered as magic literals across the codebase. Domain-specific
data that is tightly bound to a single function (e.g. block-marker substrings,
SPA-shell regexes) lives next to that function in its own module instead.
"""

from __future__ import annotations

import os

# Headful Chrome gives materially better evasion than headless; default to
# headful and fall back to headless when launch fails (e.g. a server with no
# display). Set WEBFETCH_HEADLESS=1 to force headless.
HEADLESS: bool = os.environ.get("WEBFETCH_HEADLESS", "0") == "1"

# Retry/backoff tuning, in seconds. Applied within a tier before escalating.
BACKOFF_BASE: float = 1.0
BACKOFF_CAP: float = 12.0

# Cap an honored ``Retry-After`` so a hostile/huge value cannot stall the tool.
RETRY_AFTER_CAP: float = 30.0

# A realistic Chrome header set layered on top of curl_cffi's impersonation.
DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Known image magic-byte prefixes, used to sniff binary content types when the
# Content-Type header is missing or generic.
IMAGE_MAGIC: tuple[bytes, ...] = (
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF8",
    b"RIFF",
    b"BM",
    b"\x00\x00\x01\x00",
)

# Browser context defaults, shared by every browser-backed tier and screenshot.
BROWSER_VIEWPORT: dict[str, int] = {"width": 1920, "height": 1080}
BROWSER_LOCALE: str = "en-US"
BROWSER_TIMEZONE: str = "America/New_York"

# Default navigation timeout (milliseconds) for the browser tiers.
DEFAULT_TIMEOUT_MS: int = 30_000

# Only the first N characters of a body are scanned for soft-block markers.
BLOCK_SCAN_LIMIT: int = 30_000

# Circuit breaker: per-domain thresholds. After CIRCUIT_FAIL_MAX consecutive
# failures to the same root domain, the circuit opens and requests are rejected
# immediately for CIRCUIT_RESET_TIMEOUT seconds (then half-open: one probe).
CIRCUIT_FAIL_MAX: int = 3
CIRCUIT_RESET_TIMEOUT: float = 60.0
