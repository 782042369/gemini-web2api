# Production restart and core-function smoke test

## Latest update: browser-parity hardening deployed (2026-09-08 22:54 +08:00)

Supersedes prior status below; earlier sections remain as historical evidence.

- Deployed image `3046b7266abb` (commit 8ced1bc): StreamGenerate now carries `f.sid` + live `cfb2h` bl + model-selection headers; image upload prefers one-shot `content-push` multipart (log line `Image uploaded via content-push multipart` confirms it is live); impersonation pinned to `chrome145`.
- Text probe after deploy: 200 in 2.3s, reply intact.
- Vision probe: still `BardErrorInfo [1100]` (0.5s fail-fast). Full client-side parity with HanaokaYuzu/Gemini-API is now in place; the remaining blocker is the upstream file-pipeline change documented in docs/INCIDENT-20260908-vision.md. When Google restores the pipeline this exact chain is expected to recover without further code changes.

## Prior: credential refresh + container restart (2026-09-08 12:40–12:41 +08:00)

Supersedes prior status below; earlier sections remain as historical evidence.

- Imported the user-provided refreshed Cookie (28 cookies, same account identity: SAPISID unchanged; short-lived PSIDTS/SIDCC tickets rotated). A staged isolated page fetch obtained a fresh SNlM0e token, saved into the active cookie JSON. Bind-mount inode and mode 0600 preserved; prior file backed up at cookie_pro.txt.bak-cookie-1788842417553589141.
- Restarted the production container at 12:40:41 +08:00 as explicitly requested. Image unchanged: sha256:8a211fe5f703b61286c5967b830960c18a3931f4d6b7375d5996222ae2ad3598. The pending retry/deadline/batching code is still NOT deployed.
- Logs before the update show one vision request failing with BardErrorInfo [1100] at 12:36 under the previous session state, confirming the refresh was needed.
- After restart the full self-renewal loop is observable in logs: traffic-driven Set-Cookie renewal + persistence, XSRF auto-refresh (new token on first request), and keepalive running every 540s.

| Test after refresh + restart | Result | Elapsed | Request ID |
| --- | --- | --- | --- |
| Single translation | HTTP 200, correct Chinese translation | 9.195 s (first request after restart, session warm-up) | b7807d0374c1 |
| Three-part batch translation | HTTP 200; 3/3 segments, order and newline preserved | 2.453 s | 01d39d18230c |
| Vision / OCR of verified JPEG logo | HTTP 200; reads "Gemini Web2API", describes four-point star, right arrow, braces; no repeated wording this time | 8.933 s | cf84c2cb01f7 |

Container state at final check: running, oom=false, CPU 0.02%, memory 28 MiB / 512 MiB. Temporary credential staging files were removed after verification.

## Earlier update: refreshed credentials (2026-09-08 10:02–10:05 +08:00)

## Latest update: refreshed credentials (2026-09-08 10:02–10:05 +08:00)

This section supersedes the earlier vision-failure status below; the earlier observations remain as historical evidence.

- Imported the user-provided Gemini Cookie in place, preserving the Docker bind-mount inode and mode 0600. The complete previous JSON is backed up in cookie_pro.txt.bak-cookie-1788832936863237662 (mode 0600).
- A staged, isolated app-page fetch with the new credentials obtained a fresh SNlM0e token. That token was saved into the active cookie JSON. No Cookie or token values are included here.
- The old accounts_cookie had a different SAPISID from the newly supplied session. It was removed from the active JSON rather than mixing account credentials; it remains in the protected backup. The existing keepalive fallback path is available, but long-term renewal of this new session has not yet been separately certified.
- Production container/image was not restarted or replaced: it is still sha256:8a211fe5f703b61286c5967b830960c18a3931f4d6b7375d5996222ae2ad3598, started at 07:52:35 +08:00. The pending retry/deadline/adaptive-batch code is not deployed.

| Test after credential refresh | Result | Elapsed | Request ID |
| --- | --- | --- | --- |
| Verified JPEG logo OCR / image understanding | HTTP 200; reads Gemini Web2API and describes four-point star, right arrow, braces | 17.733 s | 939805378eb0 |
| Three-part translation | HTTP 200; all 3 segments in order and first paragraph newline preserved | 2.407 s | 7ab6733c38ae |

The vision response contains some repeated wording. This verifies recovery for the test image, not perfect answer quality or a broad success-rate benchmark. Changing credentials and obtaining a fresh XSRF token restored this request on the same production image; it is not evidence that the pending code changes repaired the authentication protocol.

## Earlier restart and failure investigation

## Deployment

- Restarted at 2026-09-08 07:52:35 +08:00 using docker-compose build followed by up -d --force-recreate.
- Running container: gemini-web2api, bound to 127.0.0.1:8081.
- Running image ID: sha256:8a211fe5f703b61286c5967b830960c18a3931f4d6b7375d5996222ae2ad3598.
- Previous image retained as gemini-web2api:rollback-before-core-20260908 (sha256:28f5a24595510d82d1f63e83a8b8f6aa77d579d59a13f8f1bc63a9dcdbea23be).
- Cookie file was recently renewed (07:43:10 +08:00) before the restart. No credentials are included in this report.

## Live test results

Tests used the local production API, not mocked upstream responses. Generation requests were sequential, not a concurrency/load test.

| Test | Result | Elapsed | Request ID |
| --- | --- | --- | --- |
| GET / | HTTP 200, status=ok | — | — |
| Authenticated GET /v1/models | HTTP 200 | — | — |
| Single English-to-Chinese translation | HTTP 200, correct translation | 2.670 s | a1d8bfc8e925 |
| Three-part translation with an embedded newline | HTTP 200; 3 results in order, newline preserved | 3.131 s | 1889fa60c3e3 |
| Vision/OCR using a verified 1024×1024 JPEG logo | HTTP 502, BardErrorInfo [1100] | 7.741 s | d7e3e4264827 |

Single translation:

    The new version preserves paragraph breaks and retries incomplete segments.
    新版本保留了段落分隔，并将重试未完成的片段。

Batch translations returned:

1. 早上好。 / 火车九点开。 (the slash represents the preserved newline)
2. 请保留原始段落顺序。
3. 这张照片展示了一辆蓝色自行车。

## Vision failure investigation

- The first tiny PNG fixture was discovered to have an invalid IDAT CRC and is **not** accepted as vision verification evidence.
- The meaningful retest used the repository logo's actual JPEG bytes (the repository filename logo.png is misleading). Its visible title is Gemini Web2API. The valid JPEG still failed with BardErrorInfo [1100].
- A diagnostic app-page request returned HTTP 200 and exposed push_id/pctx, but no SNlM0e key or fresh XSRF token. Explicit auth_user=0 also did not yield that token. No tokens or page contents were logged in the report.
- A one-shot, read-only test of the retained previous image, using the same JPEG and current credential file, also failed with BardErrorInfo [1100] (one attempt, 1.267 s). This failure is not unique to the newly deployed image.
- These observations point to the upstream session/attachment binding path. The exact root cause and a fix are **not yet established**. Vision must not be reported as passed.

## Final state

The new image remains deployed because translation checks pass and the prior image exhibits the same vision failure. At the final check the container was running, restart_count=0, oom_killed=false, memory approximately 60 MiB / 512 MiB, and GET / still returned 200.

The temporary baseline container was launched with --rm and read-only configuration/credentials. The previous production image remains available for rollback.

## Repeat selected checks

These commands make live upstream requests; run individually and avoid burst probes:

    python3 scripts/smoke_core.py --case text
    python3 scripts/smoke_core.py --case batch
    python3 scripts/smoke_core.py --case vision

The script reports HTTP status, elapsed time, request ID and returned content without printing API keys or image data. Offline regression results from the previous implementation phase are not substitutes for these live results.
