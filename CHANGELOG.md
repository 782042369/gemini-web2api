# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver.

## [Unreleased]

### Vision research pass 5 (2026-09-28)

Round-5 web research (HanaokaYuzu master incl. PR #247, g4f 2026 provider state) applied to the bridge chain; details in docs/VISION_RESEARCH.md round 5.

- At-less rescue chain: when the page carries no SNlM0e (the 2026-09-24 idle-tab incident class), the in-page chain now degrades to empty-string at + reference-client bare attachments ([[ref], name], no ProcessFile) instead of hard-failing "session not ready"; successful rescues log an "atless rescue" marker.
- Fallback push id "feeds/mcudyrk2a4khkz" (g4f default, still accepted 2026) when the page omits qKIAYe, so uploads no longer abort on a missing push id alone.
- Payload alignment with the current reference client: inner[80] thinking tier (2 extended / 1 standard) and the x-goog-ext-525005358-jspb header mirroring a real per-request uuid (inner[59]).
- Soft activity heartbeat: the vision tab keepalive now also runs the reference client's _sync_activity batchexecute (ESY5D / bard_activity_enabled) inside the tab after a fresh at probe, keeping the backend session active without reloads; failures are logged and non-fatal.
- 7 new offline regressions (tests/test_vision_round5.py); suite now 870 tests, all green.

### Translation isolation and vision request controls

- Remove query values from non-POST access logs, including parser errors; retain method, safe path and status.
- Translate source paragraphs with line-leading numeric references individually. Duplicate output markers also invalidate potentially truncated predecessor blocks.
- Deliver successful microbatch members before per-item fallbacks; isolate fallback errors and use each caller's original deadline. Expired members do not start new fallback work.
- Carry request budgets through browser queueing, token lookup, CDP waits, page reloads and parallel uploads. Control failures do not trigger rescue/retry; in-page fetches have an abort timer and bounded targeted cancellation.
- Preserve selected account/auth-user in upload workers; scope reference-cache digests to account, session, media type and image bytes.
- Add offline mock and loopback regressions. Repository changes only: no production deployment or live Google verification performed.

### Vision pipeline research pass (2026-09-24)

Full research report and rationale: docs/VISION_RESEARCH.md (reverse-engineered Gemini projects x LLM-gateway image-pipeline practices).

- New image preprocessing pipeline (image_prep.py): EXIF rotation, long-edge cap (vision_max_edge_px, default 2048), HEIC/AVIF/BMP/TIFF transcode (pillow-heif optional extra), iterative budget re-encode - oversized phone photos are now compressed instead of hard-rejected by the 4 MiB bridge limit. DecompressionBomb warnings upgraded to errors.
- Shared normalization entry (server/images.py::_normalize_images) for both chains: URL download + magic-byte sniff + prepare; fixes a crash where http-linked images in bridge mode raised TypeError before any upload. Upload references cached by content hash (15 min TTL, 64-entry LRU).
- Redirect following for remote image_url (up to 3 hops, per-hop address pinning and private/metadata re-validation, loop detection) plus a single TCP-refusal retry; TLS failures stay non-retryable (docs/HARDENING.md updated).
- Bridge chain hardening: the documented single-tab lock is actually wired (bounded 120s wait); every in-page failure carries a stage/error code; ProcessFile error 7 (stale session) triggers one tab reload + retry (automates the 2026-09-24 idle-tab outage class); persistent registration failures fall back to bare-reference attachments (the reference-client form); upload filenames now carry the correct extension per MIME (g4f #3064); requested model/thinking level flows into the page payload.
- Direct-chain circuit breaker (3 consecutive failures -> 60s cooldown, auto mode goes straight to the bridge) and a reverse rescue: a failed bridge attempt with live borrowed tokens is served once through the direct chain instead of a terminal 502.
- Optional vision tab keepalive ring (vision_tab_keepalive_sec, default off): periodic CDP tab reload keeping DBSC rotation and the at token fresh; enabling it is an ops decision (KEY.md 2026-09-24 proposal B).
- Pillow added to dependencies; vision-heic optional extra; 820 offline tests (was 794), ruff clean.

### README rewrite (2026-09-20)

- Both language editions (README.md / README_CN.md) fully rewritten from repository evidence: centered identity band, architecture ASCII diagram, verified Quick Start, current model table (3.8 default), vision/vision_mode guide, cookie and DBSC notes, complete configuration reference, development workflow. No emoji; every claim traceable to the source tree.

### XSRF self-heal retry + model-routing verification (2026-09-20)

- Live matrix (9 variants against StreamGenerate) verified our model routing: body `inner[79]` routes correctly with the neutral `x-goog-ext-525001261` ticket; `inner[80]` is deliberately NOT sent (`[80]=1` was observed to break `[79]` routing) and the ticket's `[14]/[15]` slots (upstream PR#100's channel) stay null - findings documented in `models.py`.
- XSRF self-heal (from upstream Sophomoresty/gemini-web2api PR#100 §2, adapted): an upstream 400/401 XSRF rejection now invalidates every token cache layer (page tokens, account XSRF overrides, CDP bridge tokens) and retries once immediately - the at token rotates server-side within minutes, so requests no longer fail on a stale token until the 300s keepalive refresh.

### Vision direct-first hybrid with bridge token lending (2026-09-15)

- New `vision_mode` config (`auto`/`bridge`/`direct`, default `auto`): image requests prefer the direct server-side chain (upload + StreamGenerate on the curl_cffi TLS session) whenever a live token set is available, and use the CDP bridge otherwise and as fallback on upload/generate failure (non-stream).
- `vision_bridge.fetch_page_tokens()`: reads the live page tokens (`SNlM0e`/`qKIAYe`/`Ylro7b`/`FdrFJe`/`cfb2h`) from the CDP tab so the direct chain can borrow the browser-only XSRF token; wedge-tolerant (a hung tab is replaced by a freshly created one and closed), with success/failure cooldown caches.
- Server-side page fetches omit `SNlM0e` for this DBSC-bound account (verified across six header/fingerprint variants): token merges now fill the gap from the bridge, and token-cache TTLs are tiered (complete 600s, `push_id`-only 120s, empty 30s).
- Bridge path pre-flight: a logged-out or wedged tab now fails in milliseconds with `vision_bridge_not_logged_in` and a re-login hint instead of hanging for the full chain timeout (previously a 200s 502).
- Incident context: the llq CDP Chrome had been wedged for days and silently lost its Google login, which took production vision down; the instance was restarted and requires a one-time manual re-login.

### Vision restored via CDP browser bridge (2026-09-10)

- New `vision_bridge` module: image-bearing chat requests are executed inside a CDP-attached, logged-in Gemini tab (upload → ProcessFile with the live `at` token → StreamGenerate), the only environment the upstream still issues XSRF tokens to. Configured via `vision_bridge_url`; absent/None keeps the direct chain.
- Adds `websocket-client` dependency; bridge hostnames are resolved to IPs to satisfy Chrome's DevTools Host-header check.

### Browser-parity hardening (2026-09-08, from HanaokaYuzu/Gemini-API)

- StreamGenerate now sends the live page session id as `f.sid` and the freshest frontend build label (`cfb2h`) instead of the static config `gemini_bl`, plus the model-selection envelope headers (`x-goog-ext-525001261-jspb`, `73010989`, `73010990`).
- Image upload prefers a one-shot multipart POST to `content-push.googleapis.com` (the reference client's form) and keeps the two-step Scotty resumable flow as an automatic fallback.
- Page-token cache now also captures `FdrFJe`/`cfb2h`; its TTL no longer depends on the removed `SNlM0e`. Default and config impersonation pinned to `chrome145` to avoid Google's Device Bound Session Credentials experiment.

### Pending: retry, deadline and translation-batch optimization

- Classify transient HTTP/transport failures using metadata; stop unchanged Bard refusals, permanent failures, and all retries after streamed text. Honor Retry-After within a single request budget.
- Add request_deadline_sec, queue_timeout_sec and max_queued_requests; propagate remaining time through coalescing, batch workers, downloads, session refresh, uploads and generation. Restore native curl timeout options and always release acquired slots.
- Add translation_batch_max_chars and translation_batch_max_segments; preserve long singleton paragraphs while preventing oversized combined groups.
- Add focused queue/cancellation/deadline and character-budget tests. The offline runner now includes pytest parameterized tests rather than only unittest discovery.
- These follow-up changes are not deployed. A later user-provided Cookie and fresh XSRF token restored one live vision check on the existing image; see docs/SMOKE_REPORT.md for evidence and limitations.

### Core translation and image understanding

- Preserve multiline translations and source indexes; retry missing/duplicate segments and report failures instead of returning untranslated source as success.
- Respect account identity, per-group size and the lone-request fast path in translation microbatching.
- Reject malformed image data before generation, preserve detected MIME and shorten recovery from failed image session-token fetches.
- Keep additional API feature expansion out of scope; focus improvements on translation and image understanding.

### Security and reliability

- Match API routes exactly and validate nested protocol inputs before upstream side effects.
- Bound request bodies with strict chunk framing and a total read deadline.
- Pin image download connections to validated DNS answers; preserve HTTPS server identity, reject redirects, limit bytes/time and close resources on all exits. Remote URL images now fail closed when an application proxy is configured; inline images remain supported.
- Isolate account XSRF, auth-user context and persistence; serialize in-process cookie read/modify/write operations and keep keepalive startup retryable.
- Stream Responses text deltas before completion; send explicit terminal errors on partial Chat/Google/Responses streams without falsely reporting success.
- Preserve Responses function-call input history and constrain parsed calls to declared functions with valid JSON-object arguments.

### Verification

- Add adversarial wire-contract, account persistence and image download regressions.
- Guard unittest/pytest against accidental external network traffic; use the guarded runner in Make and CI.
- Add pull-request validation and compile/package-build checks; do not publish Docker images from pull requests.
- Document limits, incompatible proxy-image cases and remaining operational risks in docs/HARDENING.md.

## [1.2.1] - 2026-09-05

### Changed - streaming pipeline deduplication (behavior-preserving)

- New `_stream_upstream_chunks()` transport adapter: the curl_cffi and
  httpx code paths both surface as one decoded str chunk stream (curl
  bytes go through an incremental UTF-8 decoder so multi-byte characters
  split across chunks survive). `generate_stream` and
  `_generate_upstream` now share a single chunk-to-line-to-delta
  pipeline; the previous byte-for-byte duplicated branches (BardErrorInfo
  detection, line parsing, delta extraction, slow-walk breaker x2) are
  gone. Slow-walk breaker exception texts are unchanged (log greps rely
  on them).

### Added

- 27 unit tests for upstream pure logic (tests/test_upstream.py):
  wrb.fr response parsing and clean_text rules, retry backoff ladder
  (rate-limited / transport / exponential-with-jitter), microbatch
  eligibility and numbered-batch parsing incl. dropped-segment fallback,
  batcher dispatch/error propagation, model resolution (@think= suffix),
  SAPISIDHASH header building. Suite total: 47 tests.

## [1.2.0] - 2026-09-05

### Changed - engineering restructure (behavior-preserving)

- Adopt src/ package layout; the package is now split into focused layers:
  - upstream/ - Gemini Web protocol client: transport (curl_cffi/httpx/
    urllib ladder), cookies (multi-account pool), protocol (wire format),
    parser (wrb.fr parsing), history, concurrency, generate
    (retry/coalescing/streaming pipeline)
  - server/ - HTTP API layer: base (routing/auth/SSE) plus one mixin
    module per protocol (openai_chat, openai_responses, google)
  - logs, config (with validation), models, tools, multimodal, keepalive,
    batching at the package root
- server.py (1029 lines) and gemini.py (831 lines) dissolved; every module
  is now well under the 1000-line file limit with a single concern.
- Docker image builds from src/gemini_web2api; runtime behavior, config
  schema, log message texts and endpoint semantics are unchanged.

### Removed

- Legacy single-file entry point gemini_web2api.py (root monolith, 1112
  lines) - superseded by the package; use python -m gemini_web2api or the
  gemini-web2api console script.
- Dead helper _usage() in the former server module.

### Added

- config.validate_config() - warns on unknown keys / mistyped values at
  load time (output via stderr [config] prefix).
- Makefile with test/lint/run/docker targets; pytest + ruff dev extras and
  ruff configuration in pyproject.
- CI: lint + unit test job gating the Docker publish workflow.
- This changelog.

### Fixed

- Stale unit test expectation for the image attachment tuple shape
  (browser-captured 9-element form, in place since the BardErrorInfo 1100
  fix); the suite is green again (20 tests).

## [1.1.0] - 2026-09-05

- Full cookie self-renewal via accounts-domain RotateCookies, keepalive
  module split, session keepalive loop (see git history).

## [1.0.0] - earlier

- Initial modular package, OpenAI/Responses/Google endpoints, cookie pool,
  concurrency cap, slow-walk breaker, micro-batching (see git history).
