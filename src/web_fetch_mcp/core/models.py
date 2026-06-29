"""Core domain types shared across every layer.

These are pure value objects and exceptions with no dependency on any outer
layer (controller/service/accessor) or third-party I/O library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class FetchMode(StrEnum):
    """Strategy selector for the fetch tool.

    AUTO: Cheapest-first escalation (Tier 1 → 2 → 3) until one succeeds.
    STATIC: Tier 1 only (curl_cffi static fetch, no JavaScript rendering).
    DYNAMIC: Tier 2 only (real headful Chrome, renders JavaScript).
    STEALTH: Tier 3 only (nodriver, custom CDP to avoid automation detection).
    """

    AUTO = "auto"
    STATIC = "static"
    DYNAMIC = "dynamic"
    STEALTH = "stealth"


class OutputFormat(StrEnum):
    """Output format for the rendered fetch result.

    MARKDOWN: Readable, link-preserving markdown (default for LLM consumption).
    ARTICLE: Main-content extraction via trafilatura, strips nav/ads/boilerplate.
             Falls back to full markdown when extraction yields nothing.
    TEXT: Visible text only, no formatting or markup.
    HTML: Raw rendered HTML (for DOM inspection or downstream parsing).
    """

    MARKDOWN = "markdown"
    ARTICLE = "article"
    TEXT = "text"
    HTML = "html"


class FetchBlocked(Exception):
    """Raised when every fetch strategy was blocked or otherwise failed.

    The message should include the likely remedy (e.g. a residential proxy or a
    managed unblocker) so callers can act on it.
    """


@dataclass(slots=True)
class FetchResult:
    """Result of a single fetch attempt from any tier.

    Tier 1 (curl_cffi) populates every field. The browser tiers (Patchright and
    nodriver) only ever yield rendered HTML, so they fill ``body``/``status`` and
    leave ``raw=None`` and ``headers={}`` — the PDF/JSON/image rendering paths
    never apply to them.

    Attributes:
        body: The decoded response text (HTML, JSON, etc.).
        status: The HTTP status code, or ``0`` when the tier cannot report one
            (nodriver does not expose it).
        raw: The raw response bytes, available only from the static tier; needed
            for PDF extraction and binary content sniffing.
        headers: Lower-cased response headers, available only from the static
            tier.
        content_type: The raw ``Content-Type`` header value, if any.
    """

    body: str
    status: int
    raw: bytes | None = None
    headers: dict = field(default_factory=dict)
    content_type: str = ""
