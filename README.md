<h1 align="center">gemini-web2api</h1>

<p align="center">
  <img src="logo.png" width="180" alt="gemini-web2api logo">
</p>

<p align="center">
  Google Gemini web, reverse-engineered into an OpenAI-compatible API.<br>
  Focused on translation throughput and image understanding.
</p>

<p align="center">
  <a href="README_CN.md">中文文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license: MIT">
  <img src="https://img.shields.io/badge/python-3.8%2B-3776AB" alt="python: 3.8+">
  <img src="https://img.shields.io/badge/version-1.3.0-3.8" alt="version: 1.3.0">
  <img src="https://img.shields.io/badge/docker-ready-2496ED" alt="docker: ready">
</p>

## What this is

A single Python service that speaks the gemini.google.com web protocol and exposes it as standard APIs. No Google API key, no billing: the upstream is the same web session a browser uses. Text generation works anonymously; a cookie unlocks Pro and thinking models, and a Chrome DevTools bridge restores image understanding when the account's session tokens are browser-bound.

```
[OpenAI client / Gemini CLI / Cherry Studio / curl]
        |  /v1/chat/completions  /v1/responses  /v1beta (native)
        v
[gemini-web2api]  -- translation micro-batching, budgets, retry, XSRF self-heal
        |
        +--> direct chain: curl_cffi Chrome TLS -> gemini.google.com
        |         (page tokens, Scotty upload, StreamGenerate)
        |
        +--> vision bridge (optional): CDP into a logged-in Chrome tab
                  (borrows the browser-only SNlM0e token, runs the
                   upload -> StreamGenerate chain in the page)
```

## Features

- **OpenAI-compatible**: `/v1/chat/completions` (true token-by-token SSE streaming) and `/v1/models`
- **Gemini-native**: `/v1beta/models` and `generateContent` / `streamGenerateContent` for Gemini CLI
- **Responses API**: `/v1/responses` for OpenAI Codex-style clients
- **Image understanding**: `image_url` / `input_image` parts (data URLs and http links), served through the direct chain or the CDP vision bridge - see [Vision](#vision-image-input)
- **Translation-oriented batching**: burst single-segment requests are coalesced into one numbered upstream call; multi-segment requests split with per-segment budgets
- **Model control**: Flash 3.8 / 3.7 / 3.6, deep thinking (20k+ chars), Pro, Auto, Flash Lite, plus a `@think=N` suffix (0 = deepest, 4 = shallowest)
- **Browser-grade transport**: curl_cffi Chrome TLS fingerprint first, pooled httpx and stdlib urllib as fallbacks
- **Session resilience**: background keepalive rotates cookies and refreshes the XSRF token; an upstream XSRF rejection invalidates token caches and retries once immediately
- **Account pool**: multiple cookie files rotated per request with per-account session state
- **Privacy**: temporary chats and automatic conversation deletion after generation
- **Hard limits**: request body / image size caps, bounded queues, per-request deadlines (see [docs/HARDENING.md](docs/HARDENING.md))

## Quick Start

```bash
pip install -r requirements.txt
PYTHONPATH=src python3 -m gemini_web2api
```

Or install as a package:

```bash
pip install .
gemini-web2api
```

The server listens on `http://127.0.0.1:8081` (override with `config.json`). Verify:

```bash
curl http://127.0.0.1:8081/v1/models
curl http://127.0.0.1:8081/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Reply with exactly: OK"}]}'
```

Flash-tier models work without any cookie. Pro and thinking models need one - see [Cookies](#cookies-pro-and-thinking-models).

## Docker

```bash
docker-compose up -d --build
```

The compose file binds `127.0.0.1:8081:8081`, mounts `./config.json`, your cookie file and `./logs`, and restarts unless stopped.

## Client configuration

| Field | Value |
|-------|-------|
| Base URL | `http://127.0.0.1:8081/v1` |
| API Key | any value from `api_keys` in `config.json`; auth disabled while the list is empty |
| Model | `gemini-3.8-flash` (default) |

## Models

| Model | Notes |
|-------|-------|
| `gemini-3.8-flash` | Default, latest all-around Flash |
| `gemini-3.7-flash` / `gemini-3.6-flash` / `gemini-3.5-flash` | Earlier Flash tiers (3.5 aliases 3.6) |
| `gemini-3.5-flash-thinking` | Deep thinking, longest output (20k+ chars) |
| `gemini-3.5-flash-thinking-lite` | Dynamic thinking, adaptive depth |
| `gemini-3.1-pro` | Pro tier, requires a cookie |
| `gemini-3.1-pro-enhanced` | Experimental enhanced Pro output |
| `gemini-auto` | Server-side auto selection |
| `gemini-flash-lite` | Lightweight tier |

Any model accepts `@think=N` (`gemini-3.8-flash@think=0`) to pin thinking depth 0-4.

Model routing is carried by the request body (`inner[79]`) together with a neutral per-model ticket header; the variant slot (`inner[80]`) is deliberately not sent - sending it was observed to break routing (documented in `src/gemini_web2api/models.py`).

## Cookies (Pro and thinking models)

Export cookies from a signed-in gemini.google.com browser session into a JSON file:

```json
{
  "cookie": "__Secure-1PSID=...; __Secure-3PSID=...; SAPISID=...; ...",
  "sapisid": "..."
}
```

Point `cookie_file` at it in `config.json`. Several accounts can be pooled via `cookie_files` (round-robin per request). The service rotates what it can server-side; sessions that are device-bound (DBSC) need their browser alive - the same requirement the vision bridge already covers.

## Vision (image input)

Two serving paths, selected by `vision_mode` in `config.json`:

- **direct** (default, `auto`): the service uploads the image (Scotty resumable flow) and calls StreamGenerate on its own TLS session. Needs an XSRF token the account's page hands out; accounts whose tokens are browser-bound fall back automatically.
- **bridge** (`vision_bridge_url` set): a Chrome DevTools endpoint with a logged-in Gemini tab. The service borrows the page's live token set (wedge-tolerant: hung tabs are replaced automatically) and runs the image chain in the page. When the tab is not logged in, requests fail in milliseconds with `vision_bridge_not_logged_in` instead of hanging.

| `vision_mode` | Behavior |
|--------------|----------|
| `auto` | Direct chain first when a live token set exists; bridge otherwise and as fallback |
| `bridge` | Always the CDP tab |
| `direct` | Never the bridge |

## Configuration

All keys live in `config.json` (see `config.example.json` for the full annotated set).

| Key | Default | Purpose |
|-----|---------|---------|
| `port` / `host` | `8081` / `0.0.0.0` | Listen address |
| `api_keys` | `[]` | Bearer auth list; empty disables auth |
| `default_model` | `gemini-3.8-flash` | Model when a request omits one |
| `cookie_file` / `cookie_files` | `null` / `[]` | Single or pooled accounts |
| `proxy` | `null` | Upstream proxy for all transports |
| `impersonate` | `chrome145` | curl_cffi TLS profile |
| `vision_bridge_url` | `null` | CDP endpoint for the vision bridge |
| `vision_mode` | `auto` | Vision routing (auto/bridge/direct) |
| `keepalive_sec` | `540` | Background session refresh interval |
| `retry_attempts` / `retry_delay_sec` | `3` / `2` | Upstream retry policy |
| `request_timeout_sec` / `slow_retry_sec` | `180` / `60` | Per-attempt bounds |
| `max_concurrent_requests` | `0` | Upstream concurrency cap (0 = unlimited) |
| `translation_batch_max_chars` / `..._segments` | `12000` / `25` | Batch translation split |
| `microbatch_sec` / `microbatch_max` | `1.5` / `6` | Burst coalescing window and size |
| `max_request_body_bytes` / `max_image_bytes` | `16MiB` / `20MiB` | Hard input limits |
| `temporary_chats` / `auto_delete_history` | `false` / `false` | Upstream privacy flags |
| `log_file` / `log_retention_days` | `null` / `7` | File sink with daily rotation |

## Development

```bash
make test    # offline suite (no network; 794 tests)
make lint    # ruff with the curated ruleset from pyproject.toml
make run     # dev server from the source tree
```

Reliability notes, incident records and the hardening checklist live in [docs/](docs/).

## License

MIT - see [LICENSE](LICENSE).