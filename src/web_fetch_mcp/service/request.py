"""The fetch request value object passed through the service layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """An immutable bundle of the per-request fetch parameters.

    Lets every tier share one ``async (FetchRequest) -> FetchResult`` signature
    instead of threading positional args through the strategy registry.

    Attributes:
        url: The fully-qualified URL to fetch.
        wait_ms: Extra settle time (ms) after load, for the browser tiers.
        dismiss_selector: Optional overlay selector(s) to click after load.
        proxy: Optional proxy URL, threaded through every tier.
    """

    url: str
    wait_ms: int = 2000
    dismiss_selector: str | list[str] | None = None
    proxy: str | None = None
