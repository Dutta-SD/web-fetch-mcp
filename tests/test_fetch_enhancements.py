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
from web_fetch_mcp.service.circuit import CircuitState, DomainCircuitRegistry
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


# ---------- full escalation contract ----------


def test_escalate_full_chain_static_blocked_dynamic_blocked_stealth_succeeds():
    """Auto-mode escalates through all 3 tiers when earlier ones are blocked."""
    blocked = FetchResult(body="", status=403)
    good = FetchResult(body="<html><body>stealth content</body></html>", status=200)

    async def fake_static(req):
        return blocked

    async def fake_dynamic(req):
        return blocked

    async def fake_stealth(req):
        return good

    chain = [
        strategies.Tier("static", fake_static, strategies._not_blocked),
        strategies.Tier("dynamic", fake_dynamic, strategies._not_blocked),
        strategies.Tier("stealth", fake_stealth, strategies._not_blocked),
    ]
    result = asyncio.run(escalate(FetchRequest(url="https://x"), max_retries=0, chain=chain))
    assert result is good


def test_escalate_returns_first_tier_when_it_succeeds():
    """Auto-mode returns the cheapest tier's result without escalating."""
    good = FetchResult(body="<html><body>fast content</body></html>", status=200)
    should_not_reach = FetchResult(body="<html><body>expensive</body></html>", status=200)

    async def fake_static(req):
        return good

    async def fake_dynamic(req):
        return should_not_reach

    chain = [
        strategies.Tier("static", fake_static, strategies._not_blocked),
        strategies.Tier("dynamic", fake_dynamic, strategies._not_blocked),
    ]
    result = asyncio.run(escalate(FetchRequest(url="https://x"), max_retries=0, chain=chain))
    assert result is good  # never reached dynamic


def test_with_retry_retries_then_succeeds_on_second_attempt():
    """A tier retries within its budget before the chain escalates."""
    calls = {"n": 0}
    good = FetchResult(body="ok", status=200)

    async def flaky_tier(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return FetchResult(body="", status=503)
        return good

    runner = with_retry(max_retries=1, satisfactory=lambda r: r.status == 200)(flaky_tier)
    result = asyncio.run(runner(FetchRequest(url="https://x")))
    assert result is good
    assert calls["n"] == 2  # tried twice


def test_fetch_url_auto_mode_renders_result(monkeypatch):
    """fetch_url in auto mode dispatches through escalate and renders the result."""
    content = "<html><body><h1>Hello</h1></body></html>"
    good = FetchResult(body=content, status=200)

    async def fake_static(req):
        return good

    patched = strategies.Tier("static", fake_static, strategies._not_blocked)
    monkeypatch.setitem(strategies.TIERS, "static", patched)

    out = asyncio.run(fetch_url("https://x", mode="auto", output="markdown", max_retries=0))
    assert "Hello" in out  # render_by_type converted HTML to markdown


def test_fetch_url_auto_json_content_type_detection(monkeypatch):
    """Auto-mode correctly detects and pretty-prints JSON from a static-tier response."""
    json_body = '{"key":"value"}'
    result = FetchResult(
        body=json_body, status=200, raw=json_body.encode(),
        content_type="application/json"
    )

    async def fake_static(req):
        return result

    patched = strategies.Tier("static", fake_static, strategies._not_blocked)
    monkeypatch.setitem(strategies.TIERS, "static", patched)

    out = asyncio.run(fetch_url("https://x", mode="auto", max_retries=0))
    assert '"key": "value"' in out  # pretty-printed, not raw
    assert "\n" in out  # indented


# ---------- domain-level circuit breaker ----------


def test_extract_root_domain_standard():
    extract = DomainCircuitRegistry.extract_root_domain
    assert extract("https://www.example.com/page") == "example.com"
    assert extract("https://docs.api.example.com/v2") == "example.com"


def test_extract_root_domain_two_part_tld():
    extract = DomainCircuitRegistry.extract_root_domain
    assert extract("https://shop.example.co.uk/items") == "example.co.uk"


def test_extract_root_domain_bare():
    extract = DomainCircuitRegistry.extract_root_domain
    assert extract("https://localhost:8080/") == "localhost"


def test_circuit_starts_closed():
    reg = DomainCircuitRegistry(fail_max=3, reset_timeout=60)
    assert reg.get_state("https://example.com") == "closed"


def test_circuit_opens_after_fail_max():
    reg = DomainCircuitRegistry(fail_max=3, reset_timeout=60)
    url = "https://example.com/page1"
    reg.record_failure(url, reason="captcha")
    reg.record_failure(url, reason="captcha")
    reg.record_failure(url, reason="captcha")
    assert reg.get_state(url) == "open"
    with pytest.raises(FetchBlocked, match="circuit breaker open"):
        reg.check(url)


def test_circuit_subdomain_rolls_up_to_root():
    reg = DomainCircuitRegistry(fail_max=2, reset_timeout=60)
    reg.record_failure("https://www.example.com/a", reason="blocked")
    reg.record_failure("https://api.example.com/b", reason="blocked")
    # both subdomains hit the same root domain circuit
    assert reg.get_state("https://example.com") == "open"


def test_circuit_success_resets():
    reg = DomainCircuitRegistry(fail_max=2, reset_timeout=60)
    url = "https://example.com"
    reg.record_failure(url, reason="x")
    reg.record_failure(url, reason="x")
    assert reg.get_state(url) == "open"
    # simulate timeout elapsed by manually setting state to half-open
    reg._circuits["example.com"].state = CircuitState.HALF_OPEN
    reg.record_success(url)
    assert reg.get_state(url) == "closed"


def test_circuit_half_open_probe_failure_reopens():
    reg = DomainCircuitRegistry(fail_max=2, reset_timeout=60)
    url = "https://example.com"
    reg.record_failure(url, reason="x")
    reg.record_failure(url, reason="x")
    # force half-open (simulates timeout elapsed)
    reg._circuits["example.com"].state = CircuitState.HALF_OPEN
    # probe fails
    reg.record_failure(url, reason="still blocked")
    assert reg.get_state(url) == "open"


def test_circuit_does_not_block_different_domains():
    reg = DomainCircuitRegistry(fail_max=2, reset_timeout=60)
    for _ in range(3):
        reg.record_failure("https://bad.com/x", reason="blocked")
    assert reg.get_state("https://bad.com") == "open"
    # different domain is unaffected
    reg.check("https://good.com/page")  # should not raise


def test_circuit_manual_reset():
    reg = DomainCircuitRegistry(fail_max=2, reset_timeout=60)
    url = "https://example.com"
    reg.record_failure(url, reason="x")
    reg.record_failure(url, reason="x")
    assert reg.get_state(url) == "open"
    reg.reset(url)
    assert reg.get_state(url) == "closed"
    reg.check(url)  # should not raise
