# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver.

## [Unreleased]

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
