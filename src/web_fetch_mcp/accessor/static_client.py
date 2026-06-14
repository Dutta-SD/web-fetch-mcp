"""Tier 1: static fetch over curl_cffi with a browser-grade TLS fingerprint."""

from __future__ import annotations

from curl_cffi.requests import AsyncSession

from web_fetch_mcp.core.config import DEFAULT_HEADERS
from web_fetch_mcp.core.models import FetchResult
from web_fetch_mcp.core.proxy import proxy_for_curl


async def fetch_static(
    url: str,
    proxy: str | None = None,
    timeout: int = 25,
) -> FetchResult:
    """Fetch a URL statically with a Chrome TLS/HTTP2 fingerprint.

    Does not raise on 4xx/5xx — the status is returned for the caller's block
    detection. Populates every ``FetchResult`` field, including raw bytes and
    headers, so downstream content-type handling (JSON/PDF/image) works.

    Args:
        url: The fully-qualified URL to fetch.
        proxy: Optional proxy URL, threaded through to curl_cffi.
        timeout: Per-request timeout in seconds.

    Returns:
        A fully-populated :class:`FetchResult`.
    """
    async with AsyncSession() as s:
        r = await s.get(
            url,
            impersonate="chrome",
            headers=DEFAULT_HEADERS,
            proxies=proxy_for_curl(proxy),
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
