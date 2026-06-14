# Fetch benchmarks

A fixed set of public URLs spanning rendering models and block layers, used to
validate tier selection and the honest-failure contract. Reproduce with:

```bash
python -c "import asyncio; from web_fetch_mcp.service.fetcher import fetch_url; \
print(asyncio.run(fetch_url('https://example.com')))"
```

## What the tiers do (and do NOT) handle

This tool escalates through **passive** bot-detection layers; it does **not**
solve **interactive** challenges:

| Layer | Handled? | By |
|-------|----------|----|
| TLS / JA3-JA4 + HTTP/2 fingerprinting | yes | Tier 1 (curl_cffi) |
| JavaScript fingerprinting / SPA rendering | yes | Tier 2 (Patchright) |
| Automation-protocol (CDP) detection | yes | Tier 3 (nodriver) |
| Soft blocks (HTTP-200 challenge body that auto-resolves) | often | escalate to a browser tier |
| **Interactive CAPTCHA (reCAPTCHA / hCaptcha / Turnstile checkbox)** | **NO** | detected → `FetchBlocked` |
| IP reputation (datacenter egress flagged) | partial | needs a residential `proxy` |

**CAPTCHA walls are NOT bypassed.** When a page requires a human to solve a
challenge, the tool detects the block and raises `FetchBlocked` rather than
returning the challenge page — for those, supply a residential `proxy` or a
managed unblocker. "Beating anti-bot" here means clearing passive fingerprint
checks, not solving puzzles.

## Measured results

Run: 2026-06-14, `mode="auto"`, no proxy, from a residential macOS host
(Apple Silicon, real Chrome). Latency is a single cold-ish sample, not an
average. "Tier" is the cheapest tier that produced the result.

| # | URL | Kind | Tier | Result | Size | Time |
|---|-----|------|------|--------|------|------|
| 1 | example.com | static HTML | 1 | markdown | 183 B | 0.1 s |
| 2 | en.wikipedia.org/wiki/Tariff | SSR article | 1 | markdown | 158 KB | 0.4 s |
| 3 | api.github.com/repos/python/cpython | JSON API | 1 | pretty JSON | 6.6 KB | 0.4 s |
| 4 | jsonplaceholder.typicode.com/todos/1 | JSON API | 1 | pretty JSON | 83 B | 0.2 s |
| 5 | news.ycombinator.com | static/SSR | 1 | markdown | 10 KB | 1.1 s |
| 6 | developer.mozilla.org/en-US | SSR | 1 | markdown | 14 KB | 0.1 s |
| 7 | nextjs.org | Next.js SSR | 1 | markdown | 17 KB | 0.3 s |
| 8 | react.dev | SSG | 1 | markdown | 16 KB | 0.2 s |
| 9 | vuejs.org | VitePress SSG | 1 | markdown | 5 KB | 0.2 s |
| 10 | htmx.org | static + HTMX | 1 | markdown | 7 KB | 0.2 s |
| 11 | github.com/python/cpython | SSR + hydration | 1 | markdown | 24 KB | 1.0 s |
| 12 | blog.cloudflare.com | SSR (not challenge-gated) | 1 | markdown | 30 KB | 1.0 s |
| 13 | a CSR site with a JS soft-block gate | CSR + soft block | **2** | markdown | 37 KB | 7.5 s |
| 14 | w3.org/.../dummy.pdf | PDF | 1 | extracted text | 14 B | 0.1 s |
| 15 | this-domain-does-not-exist-zzz999.com | unreachable | — | **FetchBlocked** | — | 3.4 s |

### Reading the results

- **13 / 14 reachable sites** were served by **Tier 1** (curl_cffi) — fast
  (≤1.1 s) — confirming the cheapest-first design: the browser only spins up when
  it has to.
- **Site #13** is the one escalation: Tier 1 hit a "please wait for
  verification" JS soft block, so `auto` escalated to the browser tier (Tier 2),
  which returned the real page — at the expected browser cost (7.5 s). This is
  the soft-block-handling working as designed.
- **Unreachable host (#15)** correctly raised `FetchBlocked` (3.4 s, after the
  browser tiers' own retries) rather than returning the browser's "site can't be
  reached" error page — the honest-failure contract.
- **No interactive-CAPTCHA site is in this set**, because the tool does not solve
  them; against such a site the expected (and correct) outcome is `FetchBlocked`.

## Caveats

- Single-sample latencies from one residential IP at one point in time — not a
  statistical benchmark. Block behavior is **IP- and time-dependent**: a
  datacenter IP sees materially more challenges than the residential host used
  here, and the same site can challenge differently hour to hour.
- This measures **reachability + correct tier/format**, not bypass *rates*
  against hostile anti-bot deployments. A rigorous bypass benchmark (success %
  vs. a naive `requests.get`, across a labelled hostile set, on both residential
  and datacenter egress) is future work — see `TODO.md`.
