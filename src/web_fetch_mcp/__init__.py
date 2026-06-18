"""web-fetch-mcp: a resilient, honest-failure web fetcher for LLM agents.

Layered like a Spring-Boot app:

    controller -> service -> accessor -> core

- ``controller`` is the FastMCP boundary (the ``fetch``/``screenshot`` tools).
- ``service`` orchestrates: the retry decorator, the tier strategy registry, the
  escalation ladder, and the ``fetch_url`` facade.
- ``accessor`` performs external I/O (the three fetch tiers and the browser).
- ``core`` holds pure domain types, config, and helpers and depends on nothing
  outward.

The public API is intentionally small; import deeper modules for internals.
"""

from __future__ import annotations

from web_fetch_mcp.core.models import FetchBlocked, FetchResult

__all__ = ["FetchBlocked", "FetchResult"]
__version__ = "0.1.3"
