# web-fetch-mcp — TODO / Backlog

Future additions, roughly prioritized. Items in **In design** are actively
being specced; the rest are candidate ideas.

## Done
- [x] **Article / readability mode** — `output="article"` via trafilatura.
- [x] **Retry-After honoring** — capped server-specified wait on 429/503.
- [x] **Content-type handling** — JSON pretty-print, PDF text, image note.

## High value, not yet started
- [ ] **`web_search` tool** — completes the search → fetch → read loop the
  `fetch` docstring already references. Pluggable backend:
  - **DuckDuckGo** — free, keyless default (best-effort; unofficial scraping lib,
    breaks periodically). No official web-results API.
  - **Tavily** — LLM/agent-native search API. Returns cleaned, ranked content
    (+ optional synthesized answer with sources) and a `/extract` endpoint, so it
    minimizes post-fetch cleanup. Free tier ~1,000 credits/mo, no card; paid from
    ~$30/mo. **Best fit for this MCP** — pairs naturally with `fetch`.
    Activate via `TAVILY_API_KEY` when set. (Pricing approximate — verify.)
  - **Brave Search API** — official, independent index, clean JSON SERP. Free
    ~2k/mo (card required); paid ~$3–5 per 1k queries. Good general-purpose
    keyed SERP backend.
  - **Exa** — neural/semantic search ("find pages like this"). Best for research
    discovery rather than plain top-N results.
  - Design idea: keyless DDG default, upgrade to Tavily/Brave/Exa if an API key
    is present.
- [ ] **Batch `fetch_many(urls)`** — concurrent multi-URL fetch reusing the
  existing async browser.

## Optional ML integrations (phase 2 — all optional extras, lazily imported)

**Design rule:** the tool serves two consumer modes — (a) a capable LLM agent
that already reads + judges content natively, and (b) a standalone server/library
in a non-LLM pipeline (scraper, RAG ingestion, monitor). A good ML feature helps
the *fetch mechanics* (something a downstream consumer can't do for itself); a bad
one duplicates judgment a capable caller already does. **Never a hard dependency**
(`pip install web-fetch-mcp[ml]`), lazily imported, pretrained CPU models, and it
must respect the "fail honestly / never silently drop content" contract.

Ranked by fit:

- [ ] **ML block/challenge detection (BEST FIT)** — second-stage *confirmer* on
  top of `_is_blocked`'s substring list. The hand-maintained `_BLOCK_MARKERS`
  list has a recall gap (we found the Reddit gate by hand). A tiny text
  classifier ("real content vs block/challenge/login/paywall shell") generalizes
  to unseen gates. Helps BOTH consumer modes and deepens the project's crown
  jewel (honest failure). Use only when a page is *ambiguous* (passed substring
  check but suspiciously thin), not as a replacement — keeps the fast, zero-dep,
  interpretable path as the default and limits false positives.
  - **Recommended model: MiniLM-L6 fine-tuned → ONNX, INT8-quantized.** ~25MB
    artifact, **~4–12ms/page CPU** (negligible vs the 500–4000ms fetch it guards).
    Train/export with torch on the dev box; **ship torch-free** — runtime deps are
    only `onnxruntime` + `tokenizers` + `numpy` (optional `[ml]` extra, lazily
    imported, graceful fallback to substring-only when absent). Commit a `train/`
    script so reviewers see the fine-tune (training code, not a runtime dep).
  - **Design: structural/infra signals FIRST (language-neutral)** — short
    visible-text length, high script-tag ratio, infra markers (`cf-chl`,
    `datadome`, `px-captcha`, `/cdn-cgi/challenge-platform`), hard HTTP status —
    these catch most foreign-language gates regardless of human language. The text
    classifier is the second-stage confirmer for ambiguous cases only.
  - **Multilingual:** lexical/TF-IDF approaches are English-centric; if
    multilingual matters, start Option C from a multilingual base
    (`microsoft/Multilingual-MiniLM-L12-H384` / `bge-m3` family) — ~50MB INT8,
    ~10–25ms. Turns the weakness into a "shipped a torch-free multilingual
    classifier" portfolio line.
  - **Lighter fallback (Option A):** TF-IDF char-ngram + LogisticRegression,
    <1MB, <1ms, scikit-learn only, interpretable — right tool if the signal stays
    shallow/lexical and multilingual isn't a priority.
- [ ] **Embeddings / chunked RAG output** — a mode returning the page cleaned,
  chunked, and optionally embedded (bi-encoder, contrastive-trained, e.g.
  `BAAI/bge-small-en-v1.5`). Strong value for the standalone RAG-ingestion
  consumer (this is what Jina Reader / Firecrawl monetize). Produces vectors,
  never silently judges — ethos-safe. Good ML/RAG portfolio signal.
- [ ] **Relevance reranking** — gated on `web_search` existing. A cross-encoder
  (`cross-encoder/ms-marco-MiniLM-L-6-v2`, ~22M, CPU, ~ms) ranks "which of N
  search results to actually fetch" before paying browser cost. Valuable
  precisely for the standalone consumer (no LLM to rank for it). Advisory only:
  return scores, caller decides — never silently drop.
- [ ] **Page-type classification** (article/product/listing/forum) — minor;
  trafilatura + content-type handling already cover most of the need. Skip unless
  a consumer asks.
- [ ] **On-page summarization via Ollama/smol-LLM — SKIP.** Only helps the dumb
  consumer, heavy (Ollama service or torch), quality-limited at 0.5–1B; the one
  non-redundant case (non-LLM pipeline wanting a summary) is exactly where small
  models disappoint. Not worth the "lightweight local-first" identity shift.

Model-choice note: prefer **pretrained off-the-shelf** (cross-encoder for
relevance, bi-encoder for embeddings). Cross-encoder > contrastive bi-encoder >
Ollama for relevance. Training a custom model needs labeled data and is overkill
for a general fetcher — see implementation notes if a custom block-detector is
ever pursued (must stay <50MB, CPU-only, e.g. ONNX-exported MiniLM or a
fastText/linear head over embeddings).

## Robustness & open-source credibility (highest-leverage are NOT ML)

- [ ] **`robots.txt` awareness** — `respect_robots=True` option that checks
  robots before fetching. The single best open-source-credibility move: signals a
  responsible-scraping tool, backs the authorized-use README disclaimer, pre-empts
  ToS criticism. Cheap, high-signal.
- [ ] **Structured failure taxonomy** — replace the single `FetchBlocked` with
  subclasses: `CaptchaWall` / `LoginRequired` / `RateLimited` / `NotFound` /
  `Timeout`. Lets programmatic consumers branch on the reason. Pairs with the ML
  classifier below (which can *label* the block type).
- [ ] **Eval / benchmark harness** (`benchmarks/`) — runs the tiers against a
  fixed URL set, reports tier-hit-rate + bypass success vs a naive fetch. Turns
  the "defeats most anti-bot walls" claim into a demonstrated result, and is where
  any ML classifier earns its precision/recall numbers. Directly answers the
  assessment's "headline claim is unverified" weakness.
- [ ] **Per-domain adaptive tier memory** — remember which tier last succeeded
  for a domain and start there (skip cheap tiers known to fail for it). Cuts
  latency; light systems feature. Pairs with the TTL cache.
- [ ] **Language detection** (`fasttext-langid`, ~few MB, sub-ms) — tag each
  fetch with detected language for RAG routing/filtering; also feeds the
  multilingual block-detector. Low cost, real utility.
- [ ] **Near-duplicate detection** (MinHash/SimHash — statistical, no torch) —
  detect when two URLs return substantially the same content (mirrors, tracking-
  param variants). Useful for `fetch_many`/crawl; pairs with the TTL cache.
- [ ] **Multi-class page-state classifier (ELEGANT ML WIN)** — instead of a
  binary block detector, make the ONNX model multi-class:
  content / captcha / login / rate-limited / soft-404. ONE small model then powers
  BOTH honest-failure detection AND the structured failure taxonomy above. Catches
  HTTP-200 soft-404s (sites that return 200 for missing pages). Non-redundant with
  the caller LLM, strong portfolio signal, and the eval harness gives it real
  precision/recall. This is the recommended shape for the ML classifier.

Explicitly NOT doing: LLM summarization (redundant with caller, heavy); custom
content-extractor (trafilatura already wins); anything that silently drops or
transforms content (violates the honest-failure contract).

## Nice to have
- [ ] **Metadata extraction** — title, description, OpenGraph, JSON-LD,
  outbound links, without pulling the full body.
- [ ] **Response caching (TTL)** — avoid re-fetching the same URL within a
  window; cuts latency and repeat-hit block risk.
- [ ] **Custom headers/cookies param** — let callers pass a cookie jar or auth
  header for sites they have sessions on.
- [ ] **Optional managed-unblocker tier** — escalate to ScrapingBee / ScraperAPI
  / Bright Data / Zyte after nodriver for CAPTCHA walls the local tiers can't
  beat. (The `fetch` docstring already points users here for CAPTCHA-gated sites.)
