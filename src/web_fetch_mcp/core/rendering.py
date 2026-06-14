"""HTML/content rendering and content-type classification.

Pure CPU transforms over content already in hand (no network or browser I/O), so
per the dependency rule these belong in ``core`` rather than ``accessor`` even
though they use bs4/markdownify/trafilatura/pypdf.
"""

from __future__ import annotations

import io
import json

import pypdf
import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify as to_md

from web_fetch_mcp.core.config import IMAGE_MAGIC
from web_fetch_mcp.core.models import FetchBlocked, FetchResult


def make_soup(html: str) -> BeautifulSoup:
    """Parse HTML with lxml, falling back to the stdlib parser.

    Centralizes the parser-fallback idiom used by both content rendering and SPA
    detection so it is defined in exactly one place.

    Args:
        html: The HTML document to parse.

    Returns:
        A parsed ``BeautifulSoup`` tree.
    """
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 — lxml may be unavailable; fall back
        return BeautifulSoup(html, "html.parser")


def to_output(html: str, fmt: str) -> str:
    """Render HTML into the requested output format.

    Args:
        html: The HTML document.
        fmt: One of ``"html"``, ``"text"``, ``"article"`` or ``"markdown"``.
            ``"article"`` extracts the main content via trafilatura and falls
            back to full markdown when the page is not an article.

    Returns:
        The rendered string in the requested format.
    """
    if fmt == "html":
        return html
    if fmt == "text":
        return make_soup(html).get_text("\n", strip=True)
    if fmt == "article":
        extracted = trafilatura.extract(html, output_format="markdown")
        if extracted and extracted.strip():
            return extracted.strip()
        # Not an article (homepage/listing/etc.) -> fall back to full markdown.
        return to_md(html, heading_style="ATX")
    return to_md(html, heading_style="ATX")


def detect_content_type(result: FetchResult) -> str:
    """Classify a fetch result as ``html``, ``json``, ``pdf`` or ``image``.

    Prefers the ``Content-Type`` header; when it is missing or generic, sniffs
    magic bytes and then the body prefix.

    Args:
        result: The fetch result to classify.

    Returns:
        One of ``"html"``, ``"json"``, ``"pdf"`` or ``"image"``.
    """
    ct = (result.content_type or "").lower()
    if "application/pdf" in ct:
        return "pdf"
    if "json" in ct:
        return "json"
    if ct.startswith("image/"):
        return "image"
    # An explicit HTML/XHTML header is definitive — don't sniff a body that may
    # legitimately start with '{' (e.g. inline JSON-LD) into being JSON.
    if "html" in ct:
        return "html"
    # Sniff body/raw when the header is missing or generic (octet-stream/plain).
    raw = result.raw
    if raw:
        if raw.startswith(b"%PDF-"):
            return "pdf"
        if any(raw.startswith(m) for m in IMAGE_MAGIC):
            return "image"
    stripped = result.body.lstrip()
    if stripped[:1] in ("{", "["):
        return "json"
    return "html"


def render_by_type(result: FetchResult, output: str) -> str:
    """Render a fetch result according to its detected content type.

    JSON is pretty-printed, PDFs are text-extracted, images return a note to use
    the screenshot tool, and HTML falls through to :func:`to_output` (including
    article mode). Browser-tier results carry ``raw=None``/``headers={}`` and so
    are always classified as HTML, making this safe for every tier.

    Args:
        result: The fetch result to render.
        output: The desired output format for HTML content (see
            :func:`to_output`).

    Returns:
        The rendered string.

    Raises:
        FetchBlocked: If a PDF cannot be parsed (a corrupt/empty PDF is a real
            failure, not a soft fallback case).
    """
    kind = detect_content_type(result)
    if kind == "json":
        try:
            return json.dumps(json.loads(result.body), indent=2, ensure_ascii=False)
        except (ValueError, TypeError):
            return result.body
    if kind == "pdf":
        try:
            reader = pypdf.PdfReader(io.BytesIO(result.raw or b""))
            text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:  # noqa: BLE001 — corrupt/empty PDF is a real failure
            raise FetchBlocked(
                f"could not extract text from PDF ({type(e).__name__})"
            ) from e
        return text.strip()
    if kind == "image":
        ct = result.content_type or "image"
        return f"[{ct} — use the screenshot tool to view this URL]"
    return to_output(result.body, output)
