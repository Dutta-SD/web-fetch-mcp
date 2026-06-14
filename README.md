# web-fetch-mcp

**A web-fetch [MCP](https://modelcontextprotocol.io) server for LLM agents that
fails honestly — it raises `FetchBlocked` instead of silently handing your model
a CAPTCHA or login page as if it were the article.**

Naive fetchers poison an agent's context: when a site returns a "Just a moment…"
Cloudflare interstitial or a login wall with HTTP 200, the agent reads the
challenge page as if it were content and reasons from garbage. `web-fetch-mcp`
detects that and either escalates to a stronger strategy or fails loudly.

## How it works

A cheapest-first escalation ladder. Each tier defeats a different layer of
bot-detection, and the server only pays for the expensive ones when it has to:

| Tier | Engine | Defeats | Speed |
|------|--------|---------|-------|
| 1 | `curl_cffi` (Chrome TLS/HTTP2 fingerprint) | TLS (JA3/JA4) + HTTP/2 fingerprinting | ~500 ms |
| 2 | Patchright (real headful Chrome) | JavaScript fingerprinting; renders SPAs | ~1–3 s |
| 3 | nodriver (custom CDP) | automation-protocol (CDP) detection | ~2–4 s |

Every tier's output is checked for **hard blocks** (403/429/503) and **soft
blocks** (HTTP-200 challenge/login bodies from Cloudflare, DataDome, PerimeterX,
Imperva, …). Transient failures retry with exponential backoff + jitter
(honoring `Retry-After`) before escalating. If everything is blocked, it raises
`FetchBlocked` with a remedy hint — it never returns a block page as content.

## Tools

- **`fetch`** — retrieve a page as `markdown` / `text` / `html` / `article`
  (main-content extraction via trafilatura). Non-HTML URLs are auto-handled:
  JSON is pretty-printed, PDFs are text-extracted, images return a note to use
  `screenshot`.
- **`screenshot`** — render a page in real Chrome and return a PNG.

## Quickstart

```bash
uv sync
uv run mcp install server.py    # or run directly: uv run python server.py
```

Add to an MCP client (e.g. Claude Desktop) as a stdio server pointing at
`server.py`.

```python
fetch("https://example.com/article", output="article")   # clean main content
fetch("https://api.site/data.json")                       # pretty-printed JSON
fetch("https://spa.example.com", mode="dynamic")          # force a JS render
```

## Responsible use

This tool is for fetching content you are **authorized** to access. You are
solely responsible for complying with each site's Terms of Service, `robots.txt`,
and applicable law. It honors `Retry-After` and backs off by default; please
rate-limit responsibly. It does **not** solve CAPTCHAs or bypass authentication
you do not hold. Provided **as-is, without warranty**.

## License

[Apache-2.0](LICENSE).
