# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semver.

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
