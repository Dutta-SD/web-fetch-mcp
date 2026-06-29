"""Shared utility functions used across the codebase."""

from __future__ import annotations

from enum import StrEnum

import tldextract


def extract_root_domain(url: str) -> str:
    """Extract the registrable root domain from a URL.

    Subdomains roll up to the root: ``docs.example.com`` → ``example.com``.
    Uses the Public Suffix List via tldextract for correct handling of all
    TLDs (``co.uk``, ``com.au``, ``github.io``, etc.).

    Args:
        url: A fully-qualified URL (e.g. ``https://docs.example.co.uk/path``).

    Returns:
        The root domain string (e.g. ``"example.co.uk"``).
    """
    ext = tldextract.extract(url)
    return ext.top_domain_under_public_suffix or ext.domain


def parse_enum(enum_cls: type[StrEnum], value: str, param_name: str) -> StrEnum:
    """Validate and convert a string to an enum member, or raise ValueError."""
    try:
        return enum_cls(value)
    except ValueError:
        valid = ", ".join(e.value for e in enum_cls)
        raise ValueError(f"{param_name} must be one of [{valid}], got {value!r}") from None
