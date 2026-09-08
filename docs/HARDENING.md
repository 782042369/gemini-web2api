# Gateway hardening and verification

## Product focus: translation and image understanding

The project's primary functions are translation and image understanding. Work on additional Responses lifecycle, agent tooling and unrelated API features is out of scope. Existing endpoints remain compatible.

Translation batches preserve paragraph newlines and source order. Missing/duplicate indexes are retried individually; a failed or empty retry returns an error rather than passing the untranslated source off as a successful translation. Microbatches keep account identity, cap each dispatched group, drop cancelled pending entries and wake lone requests at their short wait threshold.

Vision requests reject malformed inline images before text-only generation can accidentally proceed. Detected image MIME reaches the Gemini attachment payload, uploads retain account context, and failed page-token fetches use a short retry interval instead of caching failure for ten minutes.

## Request contracts

- Generation routes match the complete path. A query string does not change routing or disable authentication. Arbitrary paths containing a Google action name are not generation endpoints.
- Consumed OpenAI/Responses/Google fields are checked before conversion, batching, downloading, or uploading. Invalid nested values return 400 with a field path instead of triggering an internal exception.
- OpenAI-style tools support function declarations only. Flat Responses tools and named choices are accepted. Unsupported hosted tools return 400 rather than being silently advertised as executable.
- Only declared function names can become tool calls. An explicit empty allowlist accepts none. Malformed blocks and invalid/non-object arguments remain ordinary output text.
- Responses input function-call items are retained in the flattened history. This does **not** add response storage or previous_response_id continuation support.

## Streaming behavior

Responses text streaming (no tools, or tool_choice=none) sends actual upstream text deltas as they arrive. Function-calling paths remain buffered so complete model output can be parsed safely.

Once HTTP 200 SSE headers are sent, errors cannot change the HTTP status. Chat emits an error payload followed by [DONE], Google emits an error payload, and Responses emits a sequenced response.failed event. An errored stream does not send a success finish event. SSE errors use a stable public message rather than copying upstream exceptions. X-Accel-Buffering:no and no-transform discourage proxy buffering; configure the reverse proxy consistently. Heartbeats are not yet implemented.

## Request-body limits

| Setting | Default | Meaning |
| --- | --- | --- |
| max_request_body_bytes | 16777216 (16 MiB) | Positive limit for a decoded HTTP request body |
| request_body_timeout_sec | 30 | Positive **total** body-read budget, not an idle timeout |
| max_image_bytes | 20971520 (20 MiB) | Maximum downloaded or uploaded image size |
| allow_private_image_urls | false | Explicit opt-in for ordinary private/loopback image hosts |

The HTTP reader rejects duplicate Content-Length, Transfer-Encoding plus Content-Length, unsupported encodings, signed or malformed lengths, truncated chunks and invalid/oversized trailers. Bytes after the entity are left for the next keep-alive request. Rejected framing closes the connection; oversized bodies return 413 and body deadlines return 408. The body timeout is restored before generation starts.

Upstream slot queues and pending microbatch entries are bounded separately as described below. This is **not** a global connection-admission limit: HTTP worker/header intake still needs reverse-proxy connection limits and appropriate header timeouts.

## Request deadlines, retries and adaptive translation batches

| Setting | Default | Meaning |
| --- | --- | --- |
| request_deadline_sec | 180 | One monotonic budget per HTTP POST, including body processing, queueing, image download/session refresh/upload, all generation attempts and backoff |
| queue_timeout_sec | 30 | Maximum wait at each queue, also capped by the request's remaining budget |
| max_queued_requests | 64 | Maximum waiting requests per upstream account, and maximum entries waiting in the microbatch dispatcher |
| translation_batch_max_chars | 12000 | Python-character budget per translation group, including instruction/wrapper reservation and numbered separators; **not** a token limit |
| translation_batch_max_segments | 25 | Maximum segments per explicit Google translation batch; microbatches retain their own microbatch_max count limit |

The existing request_timeout_sec remains a per-operation ceiling and slow_retry_sec remains a per-attempt slow-walk threshold. Neither can extend request_deadline_sec. An image download also retains its own 30-second cap. Oversized individual source paragraphs are sent alone without truncation; an instruction longer than the character budget likewise forces singleton groups. Grouping preserves original segment order and text.

HTTP queue exhaustion/timeouts return 503 with queue_full/queue_timeout; overall deadline exhaustion returns 504 with request_timeout. Already-open SSE streams retain HTTP 200 and end with a typed error instead of a success event. A coalesced follower's expiry never removes/cancels its owner. If a shorter-lived owner expires or is cancelled, an interested follower may compete for ownership within its original deadline; queue and upstream failures are still shared rather than blindly retried. Library streams restore thread context before every yield, including interleaved consumers. Shared microbatches use the latest active member's deadline while rejecting stale results for expired members. Timed-out pending entries are removed rather than executed later.

Retry policy only permits known transient network errors and HTTP 408/425/429/500/502/503/504 before visible output. BardErrorInfo refusals (including 1100), permanent HTTP failures and unknown programming errors are not replayed unchanged. Retry-After is respected as a minimum delay; if the request cannot afford it, it terminates rather than resetting the budget. Any packed-generation exception after its own retry policy stops the batch; only successful output with missing/ambiguous segment numbering can trigger per-segment fallback. This prevents the final 429/503 Retry-After from being bypassed by fresh fallback requests. No automatic attachment re-upload/session protocol repair is included in this change, and it does not claim to repair attachment authentication. See the latest credential-refresh result in SMOKE_REPORT.md.

The preferred curl_cffi path explicitly sets libcurl TIMEOUT_MS for streams as well as uploads. Its default stream timeout only uses a low-speed limit, which is insufficient against a trickling peer. Options are restored after the operation. Fallback httpx/urllib paths use remaining-time phase/socket caps and cooperative between-chunk checks; blocking DNS/header parsing in those fallbacks and stalled filesystem operations cannot be forcibly interrupted portably. Actual downstream writes also cap their socket timeout by remaining request time, so a client that stops reading cannot retain a slot for the old 120-second socket timeout. A complete terminal error response has at most one extra second of best-effort delivery. A total deadline reached during body reading maps to 504, whereas the independent body-only timeout remains 408. Cancellation is cooperative at waits/reads/retries and generator close. Silent downstream disconnects are not guaranteed to be observed before the next write; a client half-closing its write side remains valid HTTP. Reverse-proxy timeouts/admission controls are still required.

New defaults are source-level changes only until an approved rebuild/deployment. They do not alter the current config.json or running container automatically.

## Remote image download policy

Image downloads use a separate credential-free HTTP client, not the Google session:

1. Parse an HTTP(S) URL without embedded credentials.
2. Resolve a hostname at most once; reject mixed public/private answers.
3. Connect only to validated numeric IPv4/IPv6 addresses. No second DNS lookup or automatic reconnect is allowed.
4. For HTTPS, use the original IDNA hostname for SNI, Host and certificate validation.
5. Reject every redirect. Supply the final image URL or an inline data URL instead.
6. Bound DNS wait, TCP/TLS, response headers and body by one 30-second budget; always close sockets and responses.

Non-global, multicast, unspecified, known metadata and dangerous IPv6 transition destinations are denied. Private opt-in permits normal internal hosts but does not allow metadata or multicast destinations, and never disables TLS verification. Invalid/nonpositive download size configuration falls back to 20 MiB; the downloader also has a 100 MiB hard ceiling.

**Proxy compatibility change:** when the application's proxy setting is nonempty, remote image URL downloads fail closed. Passing a hostname to a proxy would lose the validated-address guarantee. Environment proxy variables are ignored by this downloader. Inline/base64 images remain usable, and Google upload/generation transport still honors the configured proxy. Proxy-aware pinned downloads are not implemented.

libc DNS cannot be cancelled portably. At most eight resolver daemons can outlive caller timeouts; late results never open connections. Custom NAT64 or privileged network routing cannot be detected reliably at the application layer; use outbound firewall controls as defense in depth.

## Cookie and account consistency

Account paths isolate XSRF/page tokens, persistence throttles and concurrency slots. A tokenless account in a multi-account pool does not inherit another account's global XSRF token. Single-account/global-token compatibility is retained. Cookie-file auth_user is loaded before building page/upload/generation URLs and headers. Background history deletion captures the originating account context.

Keepalive activation remains retryable when disabled, missing cookies, or thread startup fails. Cookie reads, passive renewal, main-cookie persistence and accounts-cookie synchronization share a re-entrant lock. Writes flush/fsync before publishing corresponding cache metadata; throttled in-memory renewals are preserved by accounts-cookie writes.

These guarantees are **in-process only**. Single-file Docker bind mounts still require in-place writes, so a crash or external/multiple-process writer can leave an incomplete file. The lock does not provide cross-process transactionality. Use one writer process per cookie file and keep a secure recovery copy.

## Verification without production traffic

The complete test runner uses the [dev] pytest dependency to collect both unittest-style tests and parameterized retry cases. It restricts Python DNS/socket connections to loopback and blocks native curl requests, including during collection. Tests inject fake upstream sessions. Direct pytest applies the same per-test guard. No real Google credentials are required.

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]" build
    make test PYTHON=.venv/bin/python
    .venv/bin/python -m pytest -q
    .venv/bin/ruff check src tests
    .venv/bin/python -m compileall -q src tests
    .venv/bin/python -m build

Tests cover raw chunked wire framing, pipelined keep-alive requests, nested malformed inputs, partial-stream errors, a gated first-delta-before-completion test, account isolation/concurrent file writes and DNS rebinding/slow image responses. CI runs the guarded suite, lint, compile and package build before Docker publishing; pull requests do not publish images.

A successful mocked suite or image build does not prove real upstream health. Production rollout still requires a reviewed diff, recovery plan, fresh credential checks and a small sequential canary (avoid burst probes that induce Google throttling). No source change becomes live until the production image is rebuilt and deployed.
