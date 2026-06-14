import asyncio

import pytest

from server import FetchResult, _attempt


def test_fetchresult_defaults():
    r = FetchResult(body="hi", status=200)
    assert r.body == "hi"
    assert r.status == 200
    assert r.raw is None
    assert r.headers == {}
    assert r.content_type == ""


def test_attempt_returns_result_when_satisfactory():
    r = FetchResult(body="<html>ok</html>", status=200)

    async def factory():
        return r

    out = asyncio.run(_attempt(factory, lambda res: res.status == 200, 0))
    assert out is r


def test_attempt_returns_none_when_never_satisfactory():
    r = FetchResult(body="", status=403)

    async def factory():
        return r

    out = asyncio.run(_attempt(factory, lambda res: res.status == 200, 0))
    assert out is None


from server import _retry_after_delay, _RETRY_AFTER_CAP


def test_retry_after_seconds():
    assert _retry_after_delay({"retry-after": "5"}) == 5.0


def test_retry_after_missing():
    assert _retry_after_delay({}) is None


def test_retry_after_unparseable():
    assert _retry_after_delay({"retry-after": "soon"}) is None


def test_retry_after_capped():
    assert _retry_after_delay({"retry-after": "3600"}) == _RETRY_AFTER_CAP


def test_retry_after_http_date():
    # An HTTP-date far in the future is parsed to a positive, capped delay.
    val = _retry_after_delay({"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
    assert val == _RETRY_AFTER_CAP


def test_retry_after_past_date_is_zero():
    val = _retry_after_delay({"retry-after": "Wed, 21 Oct 1999 07:28:00 GMT"})
    assert val == 0.0


import pathlib

from server import _to_output

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_article_mode_extracts_main_content():
    out = _to_output(_load("article.html"), "article")
    assert "Considered Headline About Tariffs" in out
    assert "passed on to domestic consumers" in out
    # boilerplate is stripped
    assert "SITE BANNER ADVERTISEMENT" not in out
    assert "Copyright 2026" not in out


def test_article_mode_falls_back_to_markdown_when_no_article():
    out = _to_output(_load("not_article.html"), "article")
    # extraction yields nothing usable -> full markdown fallback (links survive)
    assert out  # non-empty
    assert "One" in out or "/1" in out


from server import _detect_content_type, _render_by_type, FetchResult as _FR


def test_detect_json_by_header():
    r = _FR(body='{"a":1}', status=200, content_type="application/json")
    assert _detect_content_type(r) == "json"


def test_detect_json_by_sniff():
    r = _FR(body='  {"a": 1}', status=200)
    assert _detect_content_type(r) == "json"


def test_detect_pdf_by_magic():
    r = _FR(body="", status=200, raw=b"%PDF-1.4 ...")
    assert _detect_content_type(r) == "pdf"


def test_detect_image_by_header():
    r = _FR(body="", status=200, content_type="image/png")
    assert _detect_content_type(r) == "image"


def test_detect_html_default():
    r = _FR(body="<html><body>hi</body></html>", status=200, content_type="text/html")
    assert _detect_content_type(r) == "html"


def test_render_json_pretty_prints():
    r = _FR(body='{"b":2,"a":1}', status=200, content_type="application/json")
    out = _render_by_type(r, "markdown")
    assert '"a": 1' in out and '"b": 2' in out
    assert "\n" in out  # indented, multi-line


def test_render_json_bad_body_returns_raw():
    r = _FR(body="{not json", status=200, content_type="application/json")
    out = _render_by_type(r, "markdown")
    assert out == "{not json"


def test_render_pdf_extracts_text():
    raw = (FIXTURES / "tiny.pdf").read_bytes()
    r = _FR(body="", status=200, raw=raw, content_type="application/pdf")
    out = _render_by_type(r, "markdown")
    assert "Hello PDF tariff text" in out


def test_render_image_returns_note():
    r = _FR(body="", status=200, content_type="image/png")
    out = _render_by_type(r, "markdown")
    assert "screenshot" in out.lower()
    assert "image/png" in out


def test_render_html_uses_to_output():
    r = _FR(body="<html><body><h1>Hi</h1></body></html>", status=200,
            content_type="text/html")
    out = _render_by_type(r, "markdown")
    assert "Hi" in out


def test_render_corrupt_pdf_raises_fetchblocked():
    """A corrupt/empty PDF is a real failure -> clean FetchBlocked, not a raw pypdf error."""
    from server import FetchBlocked
    r = _FR(body="", status=200, raw=b"%PDF-1.4 garbage not a real pdf",
            content_type="application/pdf")
    with pytest.raises(FetchBlocked):
        _render_by_type(r, "markdown")


def test_detect_header_beats_body_sniff():
    """Content-Type header takes precedence over body sniffing."""
    # HTML header but a body that looks like JSON -> classified html (header wins)
    r = _FR(body='{"looks": "like json"}', status=200, content_type="text/html")
    assert _detect_content_type(r) == "html"


from server import _is_blocked, _normalize_selectors


def test_reddit_verification_gate_detected_as_soft_block():
    """HTTP 200 'Please wait for verification' interstitial is a soft block.

    Verified live: Reddit serves this gate with status 200; without detection,
    auto-mode accepts ~37 chars of garbage instead of escalating to the browser
    tier (which returns the real ~36KB page).
    """
    html = (
        "<!DOCTYPE html><html><head>"
        "<title>Reddit - Please wait for verification</title></head>"
        "<body><form></form></body></html>"
    )
    assert _is_blocked(html, 200) is True


def test_checking_your_browser_detected_as_soft_block():
    html = "<html><body>Checking your browser before accessing the site.</body></html>"
    assert _is_blocked(html, 200) is True


def test_legitimate_page_with_wait_text_not_blocked():
    """Conservative markers must not false-positive on ordinary content."""
    html = "<html><body><p>Please wait while we load your dashboard.</p></body></html>"
    assert _is_blocked(html, 200) is False


def test_normalize_selectors_variants():
    assert _normalize_selectors(None) == []
    assert _normalize_selectors("text=More") == ["text=More"]
    assert _normalize_selectors(["a", "b"]) == ["a", "b"]
