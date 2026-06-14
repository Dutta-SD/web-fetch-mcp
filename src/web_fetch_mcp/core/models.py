"""Core domain types shared across every layer.

These are pure value objects and exceptions with no dependency on any outer
layer (controller/service/accessor) or third-party I/O library.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
