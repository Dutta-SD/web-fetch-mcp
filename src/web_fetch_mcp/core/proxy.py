"""Proxy URL adapters for each fetch backend.

A single proxy URL of the form ``http[s]://[user:pass@]host:port`` is reshaped
into whatever each backend expects, so the parsing logic lives in one place.
"""

from __future__ import annotations

from urllib.parse import urlparse


def proxy_for_curl(proxy: str | None) -> dict | None:
    """Shape a proxy URL for curl_cffi.

    Args:
        proxy: The proxy URL, or ``None``.

    Returns:
        A ``{"http": ..., "https": ...}`` mapping, or ``None`` when no proxy.
    """
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def proxy_for_playwright(proxy: str | None) -> dict | None:
    """Shape a proxy URL for Playwright, splitting credentials out.

    Args:
        proxy: The proxy URL, or ``None``.

    Returns:
        A ``{"server", "username"?, "password"?}`` mapping, or ``None``.
    """
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


def proxy_for_nodriver(proxy: str | None) -> str | None:
    """Shape a proxy URL into a Chrome ``--proxy-server`` argument value.

    Args:
        proxy: The proxy URL, or ``None``.

    Returns:
        A ``"scheme://host:port"`` string for ``--proxy-server``, or ``None``.
    """
    if not proxy:
        return None
    p = urlparse(proxy)
    host = f"{p.hostname}:{p.port}" if p.port else (p.hostname or "")
    return f"{p.scheme}://{host}"
