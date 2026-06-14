# Fetch benchmark sites

A fixed set of public URLs spanning rendering models and anti-bot layers, used to
validate tier selection and the honest-failure contract. "Expected tier" is the
cheapest tier expected to return usable content in `auto` mode; "Expected result"
is what a correct fetch produces. The future eval harness (see `TODO.md`) records
the **actual** tier + outcome here and diffs against expected.

Run a manual spot check with:

```bash
python -c "import asyncio; from web_fetch_mcp.service.fetcher import fetch_url; \
print(asyncio.run(fetch_url('https://example.com')))"
```

| # | URL | Kind | Expected tier | Expected result | Actual |
|---|-----|------|---------------|-----------------|--------|
| 1 | https://example.com | static HTML | 1 static | markdown with "Example Domain" | _tbd_ |
| 2 | https://en.wikipedia.org/wiki/Tariff | SSR article | 1 static | long markdown; `article` mode ~half size | _tbd_ |
| 3 | https://api.github.com/repos/python/cpython | JSON API | 1 static | pretty-printed JSON (indented) | _tbd_ |
| 4 | https://jsonplaceholder.typicode.com/todos/1 | JSON API | 1 static | pretty-printed JSON | _tbd_ |
| 5 | https://news.ycombinator.com/ | static/SSR | 1 static | markdown with story links | _tbd_ |
| 6 | https://developer.mozilla.org/en-US/ | SSR | 1 static | markdown content | _tbd_ |
| 7 | https://nextjs.org/ | Next.js SSR | 1 static | markdown (content in SSR HTML) | _tbd_ |
| 8 | https://react.dev/ | SSG | 1 static | markdown content | _tbd_ |
| 9 | https://vuejs.org/ | VitePress SSG | 1 static | markdown content | _tbd_ |
| 10 | https://app.netlify.com/ | empty CSR shell | 2 dynamic | escalates past SPA shell; rendered content | _tbd_ |
| 11 | https://www.reddit.com/r/programming/ | CSR + soft block | 2 dynamic | escalates past "please wait" gate; real content | _tbd_ |
| 12 | https://blog.cloudflare.com/ | SSR behind CF | 1 static (or 2) | markdown; not a "Just a moment" page | _tbd_ |
| 13 | https://github.com/python/cpython | SSR + hydration | 1 static | markdown repo page | _tbd_ |
| 14 | https://htmx.org/ | static + HTMX | 1 static | markdown content | _tbd_ |
| 15 | (a known PDF URL) | PDF | 1 static | extracted PDF text | _tbd_ |

Notes:
- Block behavior is IP- and time-dependent; a datacenter IP sees more challenges
  than residential. Record the egress context when filling in "Actual".
- Sites that hard-CAPTCHA every tier should surface as a clean `FetchBlocked`
  (honest failure) — that is a PASS for the contract, not a regression.
