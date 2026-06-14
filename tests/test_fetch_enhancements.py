"""Unit tests for the web_fetch_mcp core domain + retry decorator.

These cover the pure, fast, deterministic logic — content-type detection and
rendering, Retry-After parsing, article extraction, block detection, selector
normalization, and the retry decorator's stop/exhaust behavior. The browser-
driven tiers and the FastMCP controller are exercised by integration/smoke
checks, not here.
"""

import asyncio
import pathlib

import pytest

from web_fetch_mcp.core.backoff import normalize_selectors, retry_after_delay
from web_fetch_mcp.core.config import RETRY_AFTER_CAP
from web_fetch_mcp.core.detection import is_blocked
from web_fetch_mcp.core.models import FetchBlocked, FetchResult
from web_fetch_mcp.core.rendering import detect_content_type, render_by_type, to_output
from web_fetch_mcp.service import strategies
from web_fetch_mcp.service.escalation import build_auto_chain, escalate
from web_fetch_mcp.service.fetcher import fetch_url
from web_fetch_mcp.service.request import FetchRequest
from web_fetch_mcp.service.retry import with_retry

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------- FetchResult ----------


def test_fetchresult_defaults():
    result = FetchResult(body="hi", status=200)
    assert result.body == "hi"
    assert result.status == 200
    assert result.raw is None
    assert result.headers == {}
    assert result.content_type == ""


# ---------- with_retry decorator (replaces the old _attempt) ----------


def test_with_retry_returns_result_when_satisfactory():
    expected = FetchResult(body="<html>ok</html>", status=200)

    async def tier():
        return expected

    runner = with_retry(max_retries=0, satisfactory=lambda r: r.status == 200)(tier)
    assert asyncio.run(runner()) is expected


def test_with_retry_returns_none_when_never_satisfactory():
    blocked = FetchResult(body="", status=403)

    async def tier():
        return blocked

    runner = with_retry(max_retries=0, satisfactory=lambda r: r.status == 200)(tier)
    assert asyncio.run(runner()) is None


def test_with_retry_retries_until_satisfactory():
    calls = {"n": 0}
    good = FetchResult(body="ok", status=200)

    async def flaky():
        calls["n"] += 1
        return FetchResult(body="", status=503) if calls["n"] == 1 else good

    runner = with_retry(max_retries=1, satisfactory=lambda r: r.status == 200)(flaky)
    # max_retries=1 with a 503 carrying no Retry-After sleeps a jittered backoff;
    # acceptable for a unit test (sub-second) and exercises the retry path.
    assert asyncio.run(runner()) is good
    assert calls["n"] == 2


# ---------- Retry-After parsing ----------


def test_retry_after_seconds():
    assert retry_after_delay({"retry-after": "5"}) == 5.0


def test_retry_after_missing():
    assert retry_after_delay({}) is None


def test_retry_after_unparseable():
    assert retry_after_delay({"retry-after": "soon"}) is None


def test_retry_after_capped():
    assert retry_after_delay({"retry-after": "3600"}) == RETRY_AFTER_CAP


def test_retry_after_http_date():
    # An HTTP-date far in the future is parsed to a positive, capped delay.
    val = retry_after_delay({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert val == RETRY_AFTER_CAP


def test_retry_after_past_date_is_zero():
    val = retry_after_delay({"retry-after": "Wed, 21 Oct 1999 07:28:00 GMT"})
    assert val == 0.0


# ---------- article mode ----------


def test_article_mode_extracts_main_content():
    out = to_output(_load("article.html"), "article")
    assert "Considered Headline About Tariffs" in out
    assert "passed on to domestic consumers" in out
    # boilerplate is stripped
    assert "SITE BANNER ADVERTISEMENT" not in out
    assert "Copyright 2026" not in out


def test_article_mode_falls_back_to_markdown_when_no_article():
    out = to_output(_load("not_article.html"), "article")
    # extraction yields nothing usable -> full markdown fallback (links survive)
    assert out  # non-empty
    assert "One" in out or "/1" in out


# ---------- content-type detection ----------


def test_detect_json_by_header():
    r = FetchResult(body='{"a":1}', status=200, content_type="application/json")
    assert detect_content_type(r) == "json"


def test_detect_json_by_sniff():
    # Body-sniffing only applies to static-tier responses, which always carry
    # raw bytes (a missing/generic Content-Type header, JSON-looking body).
    body = '  {"a": 1}'
    r = FetchResult(body=body, status=200, raw=body.encode())
    assert detect_content_type(r) == "json"


def test_browser_tier_json_like_body_is_html_not_json():
    """A browser-tier result (raw=None) starting with '{'/'[' must stay HTML.

    Browser tiers bypassed content-type detection before the refactor; gating
    the body-sniff on raw bytes preserves that — they are never mis-rendered as
    pretty-printed JSON regardless of how the rendered DOM serializes.
    """
    r = FetchResult(body="[1, 2, 3]", status=0, raw=None)
    assert detect_content_type(r) == "html"


def test_detect_pdf_by_magic():
    r = FetchResult(body="", status=200, raw=b"%PDF-1.4 ...")
    assert detect_content_type(r) == "pdf"


def test_detect_image_by_header():
    r = FetchResult(body="", status=200, content_type="image/png")
    assert detect_content_type(r) == "image"


def test_detect_html_default():
    r = FetchResult(body="<html><body>hi</body></html>", status=200, content_type="text/html")
    assert detect_content_type(r) == "html"


def test_detect_header_beats_body_sniff():
    """Content-Type header takes precedence over body sniffing."""
    # HTML header but a body that looks like JSON -> classified html (header wins)
    r = FetchResult(body='{"looks": "like json"}', status=200, content_type="text/html")
    assert detect_content_type(r) == "html"


# ---------- content-type rendering ----------


def test_render_json_pretty_prints():
    r = FetchResult(body='{"b":2,"a":1}', status=200, content_type="application/json")
    out = render_by_type(r, "markdown")
    assert '"a": 1' in out and '"b": 2' in out
    assert "\n" in out  # indented, multi-line


def test_render_json_bad_body_returns_raw():
    r = FetchResult(body="{not json", status=200, content_type="application/json")
    out = render_by_type(r, "markdown")
    assert out == "{not json"


def test_render_pdf_extracts_text():
    raw = (FIXTURES / "tiny.pdf").read_bytes()
    r = FetchResult(body="", status=200, raw=raw, content_type="application/pdf")
    out = render_by_type(r, "markdown")
    assert "Hello PDF tariff text" in out


def test_render_image_returns_note():
    r = FetchResult(body="", status=200, content_type="image/png")
    out = render_by_type(r, "markdown")
    assert "screenshot" in out.lower()
    assert "image/png" in out


def test_render_html_uses_to_output():
    r = FetchResult(body="<html><body><h1>Hi</h1></body></html>", status=200,
                    content_type="text/html")
    out = render_by_type(r, "markdown")
    assert "Hi" in out


def test_render_corrupt_pdf_raises_fetchblocked():
    """A corrupt/empty PDF is a real failure -> clean FetchBlocked, not a raw pypdf error."""
    r = FetchResult(body="", status=200, raw=b"%PDF-1.4 garbage not a real pdf",
                    content_type="application/pdf")
    with pytest.raises(FetchBlocked):
        render_by_type(r, "markdown")


# ---------- block detection ----------


def test_verification_gate_detected_as_soft_block():
    """An HTTP-200 'Please wait for verification' interstitial is a soft block.

    Some sites serve this JS verification gate with status 200; without
    detection, auto mode would accept the tiny gate page instead of escalating
    to a browser tier that returns the real content.
    """
    html = (
        "<!DOCTYPE html><html><head>"
        "<title>Please wait for verification</title></head>"
        "<body><form></form></body></html>"
    )
    assert is_blocked(html, 200) is True


def test_checking_your_browser_detected_as_soft_block():
    html = "<html><body>Checking your browser before accessing the site.</body></html>"
    assert is_blocked(html, 200) is True


def test_legitimate_page_with_wait_text_not_blocked():
    """Conservative markers must not false-positive on ordinary content."""
    html = "<html><body><p>Please wait while we load your dashboard.</p></body></html>"
    assert is_blocked(html, 200) is False


def test_browser_unreachable_error_page_detected_as_block():
    """A browser tier rendering a 'site can't be reached' error page (status 0)
    must be flagged as blocked, so the fetch fails honestly instead of returning
    the browser's own error interstitial as if it were content.
    """
    html = (
        "<html><body><h1>This site can’t be reached</h1>"
        "<p>example.invalid’s server IP address could not be found. "
        "ERR_NAME_NOT_RESOLVED</p></body></html>"
    )
    assert is_blocked(html, 0) is True


# ---------- selector normalization ----------


def test_normalize_selectors_variants():
    assert normalize_selectors(None) == []
    assert normalize_selectors("text=More") == ["text=More"]
    assert normalize_selectors(["a", "b"]) == ["a", "b"]


# ---------- service orchestration: chain building ----------


def test_build_auto_chain_full_order_without_dismiss():
    chain = build_auto_chain(None)
    assert [t.name for t in chain] == ["static", "dynamic", "stealth"]


def test_build_auto_chain_drops_static_when_dismiss_selector():
    # The static tier cannot click overlays, so a dismiss_selector drops it.
    chain = build_auto_chain("text=Accept")
    assert [t.name for t in chain] == ["dynamic", "stealth"]


# ---------- service orchestration: escalation ----------


def test_escalate_skips_static_spa_shell_and_uses_dynamic(monkeypatch):
    """An unrendered SPA shell is 'not blocked' yet must escalate static->dynamic.

    Pins that escalate() applies the strict _static_ok predicate to the static
    tier (so an empty mount-point shell is treated as insufficient).
    """
    shell = FetchResult(body='<html><body><div id="root"></div></body></html>', status=200)
    rendered = FetchResult(body="<html><body><h1>Real content here</h1></body></html>", status=200)

    async def fake_static(req):
        return shell

    async def fake_dynamic(req):
        return rendered

    monkeypatch.setattr(strategies, "_run_static", fake_static)
    monkeypatch.setattr(strategies, "_run_dynamic", fake_dynamic)

    chain = [strategies.Tier("static", fake_static, strategies._not_blocked),
             strategies.Tier("dynamic", fake_dynamic, strategies._not_blocked)]
    result = asyncio.run(
        escalate(FetchRequest(url="https://x"), max_retries=0, chain=chain)
    )
    assert result is rendered


def test_escalate_raises_fetchblocked_when_all_tiers_fail():
    blocked = FetchResult(body="", status=403)

    async def fake_blocked(req):
        return blocked

    chain = [strategies.Tier("static", fake_blocked, strategies._not_blocked)]
    with pytest.raises(FetchBlocked):
        asyncio.run(escalate(FetchRequest(url="https://x"), max_retries=0, chain=chain))


# ---------- service orchestration: fetch_url facade validation ----------


def test_fetch_url_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must be"):
        asyncio.run(fetch_url("https://x", mode="bogus"))


def test_fetch_url_rejects_invalid_output():
    with pytest.raises(ValueError, match="output must be"):
        asyncio.run(fetch_url("https://x", output="bogus"))


def test_fetch_url_rejects_dismiss_selector_with_static_mode():
    with pytest.raises(ValueError, match="dismiss_selector requires a browser mode"):
        asyncio.run(fetch_url("https://x", mode="static", dismiss_selector="text=Accept"))


def test_fetch_url_single_tier_blocked_raises_fetchblocked(monkeypatch):
    async def fake_blocked(req):
        return FetchResult(body="", status=403)

    # Tier is frozen, so swap the whole registry entry rather than mutate it.
    patched = strategies.Tier("static", fake_blocked, strategies._not_blocked)
    monkeypatch.setitem(strategies.TIERS, "static", patched)
    with pytest.raises(FetchBlocked):
        asyncio.run(fetch_url("https://x", mode="static", max_retries=0))
