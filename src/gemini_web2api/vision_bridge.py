"""CDP vision bridge: run the image chain inside a logged-in Gemini tab.

Google's web pipeline only hands the XSRF token (SNlM0e) to a genuinely
signed-in browser session, so image understanding is executed inside the
Gemini page of a CDP-attached Chrome (the llq desktop seat) that a real
account has logged into. The bridge uploads each image, registers it via
ProcessFile (receiving the attachment UUID) and sends StreamGenerate with
the at token — the exact chain the web client uses.

Hardening (2026-09 research pass):
- Chains are serialized by _bridge_lock (bounded wait) so concurrent
  evaluates never interleave on one tab.
- Every in-page failure carries a stage tag; stale-session failures
  (ProcessFile code 7) trigger one page reload + retry before giving up,
  automating the 2026-09-24 idle-tab incident class.
- Images are preprocessed (downscale/re-encode) to fit the CDP evaluate
  payload limit instead of being rejected outright.
- Hybrid chain (research round 2): images upload server-side on the
  browser session (push_id borrowed from the page) and only ProcessFile
  + StreamGenerate run in-page, shrinking the CDP evaluate payload from
  base64 image bytes to plain reference strings and lifting the 4 MiB
  evaluate ceiling; upload failures fall back to the in-page base64
  chain re-fitted to the 4 MiB budget.
- The requested model id and thinking level flow into the page payload
  (they were silently dropped before).
"""
import base64
import json
import socket
import threading
import time
import urllib.request
import uuid
from typing import Optional

from .budget import (
    RequestBudget,
    RequestControlError,
    budget_lock,
    budget_scope,
    check_budget,
    remaining_timeout,
)
from .config import CONFIG
from .logs import log
from .vision_control import bridge_sleep, parallel_uploads, receive_cdp, request_scoped

# One browser tab executes one chain at a time; the page-level tokens are
# session-scoped and concurrent evaluates would interleave.
_bridge_lock = threading.Lock()

# How long a queued chain waits for the tab before failing (seconds).
_BRIDGE_CHAIN_WAIT = 120.0
# One full in-page chain (uploads + ProcessFile + StreamGenerate) budget.
_BRIDGE_CHAIN_TIMEOUT = 180.0
# Page reload + WIZ_global_data settle budget for the self-heal retry.
_BRIDGE_RELOAD_WAIT = 45.0

# Evaluate payloads beyond this size get slow/fragile through CDP.
MAX_BRIDGE_IMAGE_BYTES = 4 * 1024 * 1024


class VisionBridgeError(RuntimeError):
    """Raised when the browser bridge cannot complete an image request."""


def vision_bridge_enabled() -> bool:
    """Report whether a bridge endpoint is configured.

    Args:
        None.

    Returns:
        True when CONFIG["vision_bridge_url"] is set to a non-empty string.
    """
    return bool(CONFIG.get("vision_bridge_url"))


def _bridge_endpoint() -> tuple:
    """Resolve the configured bridge URL into an IP-based endpoint.

    Chrome's DevTools server rejects Host headers that are neither an IP
    nor localhost (DNS-rebinding protection), so docker container names in
    the configured URL are resolved to their current IP first.

    Args:
        None.

    Returns:
        (http_base, host, port) with host as a literal IP string.
    """
    from urllib.parse import urlparse
    raw = CONFIG["vision_bridge_url"].rstrip("/")
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    try:
        host = socket.gethostbyname(host)
    except RequestControlError:
        raise
    except Exception:
        pass
    port = parsed.port or 80
    return f"http://{host}:{port}", host, port


def _cdp_http(path: str, timeout: float = 10) -> object:
    """Call a CDP HTTP endpoint on the configured bridge.

    Args:
        path: Endpoint path starting with "/" (e.g. "/json/list").
        timeout: HTTP timeout in seconds.

    Returns:
        Parsed JSON value.

    Raises:
        VisionBridgeError: on connection or parsing failure.
    """
    check_budget("vision CDP connect")
    base = _bridge_endpoint()[0]
    try:
        with urllib.request.urlopen(base + path, timeout=remaining_timeout(timeout, "vision CDP HTTP")) as resp:
            from .upstream.transport import read_urllib_response
            return json.loads(read_urllib_response(resp, 'vision CDP HTTP').decode())
    except RequestControlError:
        raise
    except Exception as exc:
        check_budget('vision CDP HTTP')
        raise VisionBridgeError(f'vision bridge unreachable: {exc}') from exc


def _find_gemini_tab() -> dict:
    """Locate the logged-in Gemini tab through the bridge.

    Args:
        None.

    Returns:
        Target descriptor dict with webSocketDebuggerUrl.

    Raises:
        VisionBridgeError: when no Gemini tab is open on the browser.
    """
    for tab in _cdp_http("/json/list"):
        if tab.get("type") == "page" and "gemini.google.com" in tab.get("url", ""):
            return tab
    raise VisionBridgeError(
        "no gemini.google.com tab open in the bridge browser; open one and log in")


def _page_chain_js(prompt: str, images: list,
                   model_id: Optional[int] = None,
                   think_mode: Optional[int] = None,
                   allow_bare_ref: bool = False) -> str:
    """Build the in-page chain script (upload -> ProcessFile -> StreamGenerate).

    Args:
        prompt: user prompt text (already JSON-escaped by json.dumps).
        images: list of (image_bytes_or_ref, mime_type) tuples; a str
            first element is a pre-uploaded /contrib_service reference
            (hybrid chain) and skips the in-page upload step.
        model_id: optional MODE_CATEGORY id for inner[79] (1=FAST when
            omitted, the historical bridge behavior).
        think_mode: optional thinking level for inner[17] (0 = dynamic).
        allow_bare_ref: skip ProcessFile and send bare references - the
            reference-client attachment form (see module docstring).

    Returns:
        JavaScript source string for Runtime.evaluate with awaitPromise.
        Every error return carries a stage tag ("session", "upload_start",
        "upload", "process_file", "generate", "exception") so the caller
        can classify the failure; ProcessFile also extracts Google's
        numeric error code (7 = stale at token / session).
        allow_bare_ref skips ProcessFile entirely and sends uploaded
        references straight to StreamGenerate - the form reference
        clients (HanaokaYuzu/Gemini-API, Sophomoresty/gemini-web2api)
        use, kept as the fallback when registration rejects.
    """
    parts = []
    for data, mime in images:
        check_budget("vision image preparation")
        if isinstance(data, str):  # pre-uploaded file reference (hybrid)
            parts.append({"ref": data, "mime": mime})
        else:
            b64 = base64.b64encode(data).decode()
            parts.append({"b64": b64, "mime": mime})
    payload = json.dumps({"prompt": prompt, "images": parts,
                          "model_id": model_id, "think_mode": think_mode,
                          "bare_ref": bool(allow_bare_ref)})
    return """(async function () {
  var PAYLOAD = %s;
  function out(o) { return JSON.stringify(o); }
  try {
    function wiz(key) {
      var m = document.documentElement.innerHTML.match(new RegExp('"' + key + '":"([^"]+)"'));
      return m ? m[1] : null;
    }
    var at = wiz('SNlM0e'), fsid = wiz('FdrFJe'), bl = wiz('cfb2h');
    var pushId = wiz('qKIAYe'), pctx = wiz('Ylro7b');
    if (!at || !pushId) return out({stage: 'session', err: 'session not ready (no at/push_id; logged in?)'});
    var entries = [];
    var EXT = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp',
               'image/gif': 'gif', 'image/bmp': 'bmp',
               'image/heic': 'heic', 'image/heif': 'heic', 'image/avif': 'avif'};
    for (var i = 0; i < PAYLOAD.images.length; i++) {
      var img = PAYLOAD.images[i];
      var name = 'image_' + (i + 1) + '.' + (EXT[img.mime] || 'png');
      var ref;
      if (img.ref) {
        ref = img.ref;
      } else {
      var bytes = Uint8Array.from(atob(img.b64), function (c) { return c.charCodeAt(0); });
      var r1 = await fetch('https://push.clients6.google.com/upload/', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                  'Push-ID': pushId, 'X-Client-Pctx': pctx,
                  'X-Goog-Upload-Command': 'start', 'X-Goog-Upload-Protocol': 'resumable',
                  'X-Goog-Upload-Header-Content-Length': String(bytes.length),
                  'X-Tenant-Id': 'bard-storage'},
        body: 'File name: ' + name
      });
      var putUrl = r1.headers.get('x-goog-upload-url');
      if (!putUrl) return out({stage: 'upload_start', err: 'upload start failed ' + r1.status});
      var r2 = await fetch(putUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
                  'Push-ID': pushId, 'X-Client-Pctx': pctx,
                  'X-Goog-Upload-Command': 'upload, finalize',
                  'X-Goog-Upload-Offset': '0', 'X-Tenant-Id': 'bard-storage'},
        body: bytes
      });
      ref = (await r2.text()).trim();
      if (ref.indexOf('/contrib') !== 0) return out({stage: 'upload', err: 'upload failed: ' + ref.slice(0, 80)});
      }
      if (PAYLOAD.bare_ref) {
        entries.push([[ref, 1, null, img.mime], name]);
        continue;
      }
      var pfInner = [[[ref, null, 1, img.mime], name], null, 1, ['zh-CN']];
      var pfParams = new URLSearchParams();
      pfParams.set('f.req', JSON.stringify([null, JSON.stringify(pfInner)]));
      pfParams.set('at', at);
      var pfUrl = 'https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/ProcessFile?bl='
        + encodeURIComponent(bl) + '&f.sid=' + fsid + '&hl=zh-CN&_reqid=' + (Date.now() %% 1000000) + '&rt=c';
      var r3 = await fetch(pfUrl, {method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Same-Domain': '1'},
        body: pfParams.toString()});
      var pfText = await r3.text();
      var um = pfText.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/);
      if (!um) {
        var code = pfText.match(/\\[\\s*([0-9]{1,3})\\s*\\]/);
        return out({stage: 'process_file', code: code ? Number(code[1]) : null,
                    err: 'ProcessFile failed: ' + pfText.slice(0, 100)});
      }
      entries.push([[ref, 1, null, img.mime, um[0]], name]);
    }
    var inner = new Array(102).fill(null);
    inner[0] = [PAYLOAD.prompt, 0, null, entries, null, null, 0];
    inner[1] = ['zh-CN']; inner[6] = [0]; inner[7] = 1; inner[10] = 1; inner[11] = 0;
    inner[17] = [[PAYLOAD.think_mode == null ? 0 : PAYLOAD.think_mode]];
    inner[18] = 0; inner[27] = 1; inner[30] = [4]; inner[41] = [2];
    inner[53] = 0; inner[59] = 'BRDG' + Date.now().toString(16).toUpperCase() + '-4A01-4C22-9F61A7E89B01';
    inner[61] = []; inner[68] = 1;
    inner[79] = PAYLOAD.model_id == null ? 1 : PAYLOAD.model_id;
    var params = new URLSearchParams();
    params.set('f.req', JSON.stringify([null, JSON.stringify(inner)]));
    params.set('at', at);
    var sgUrl = 'https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?bl='
      + encodeURIComponent(bl) + '&hl=zh&_reqid=' + (Date.now() %% 1000000) + '&rt=c&f.sid=' + fsid;
    var r4 = await fetch(sgUrl, {method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Same-Domain': '1'},
      body: params.toString()});
    var sgText = await r4.text();
    if (sgText.indexOf('BardErrorInfo') >= 0) {
      var em = sgText.match(/BardErrorInfo[^0-9]*(\\d+)/);
      return out({stage: 'generate', err: 'upstream rejected: ' + (em ? em[1] : 'unknown')});
    }
    return out({sg: sgText});
  } catch (e) {
    return out({stage: 'exception', err: 'chain exception: ' + String(e).slice(0, 150)});
  }
})()""" % payload


def _classify_chain_failure(data: dict) -> str:
    """Classify one in-page chain failure for the retry policy.

    Args:
        data: parsed chain result dict with "stage"/"code"/"err" keys.

    Returns:
        One of "stale_session" (reload + retry), "transient" (retry on a
        fresh tab), "fatal" (surface the error immediately).
    """
    stage = data.get("stage") or ""
    code = data.get("code")
    if stage == "process_file" and (code in (7, 8) or "session" in (data.get("err") or "")):
        return "stale_session"  # documented 2026-09-24 idle-tab expiry
    if stage in ("upload_start", "upload", "exception"):
        return "transient"
    return "fatal"


def _run_page_chain(js: str) -> dict:
    """Execute one chain script on the current Gemini tab.

    Args:
        js: chain script from _page_chain_js.

    Returns:
        Parsed result dict ({sg: ...} or {stage/err/code: ...}).

    Raises:
        VisionBridgeError: when no tab exists or the evaluate fails at
        the transport level.
    """
    check_budget('vision page chain')
    try:
        tab = _find_gemini_tab()
    except VisionBridgeError:
        check_budget('vision tab recovery')
        tab = _open_fresh_gemini_tab()
    timeout = remaining_timeout(_BRIDGE_CHAIN_TIMEOUT, 'vision page chain')
    key = 'geminiBridge_' + uuid.uuid4().hex
    expression = (
        '(async function(){const controller=new AbortController();'
        f'const key={json.dumps(key)};globalThis[key]=controller;'
        f'const timer=setTimeout(()=>controller.abort(),{max(1, int(timeout * 1000))});'
        'const fetch=(url,options)=>globalThis.fetch(url,{...options,signal:controller.signal});'
        f'try{{return await {js};}}finally{{clearTimeout(timer);delete globalThis[key];}}'
        '})()'
    )
    try:
        raw = _tab_evaluate(tab, expression, timeout)
    except BaseException:
        # Best effort and bounded: abort only this chain, never another tab task.
        with budget_scope(RequestBudget(seconds=0.1)):
            try:
                _tab_evaluate(tab, f'globalThis[{json.dumps(key)}]?.abort()', 0.1)
            except Exception:
                pass
        raise
    check_budget('vision page chain completion')
    if not (isinstance(raw, str) and raw.startswith('{')):
        raise VisionBridgeError('bridge chain returned malformed output')
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VisionBridgeError(f'bridge chain output not JSON: {exc}') from exc


@request_scoped
def _reload_gemini_tab() -> dict:
    """Reload the Gemini tab and wait for fresh WIZ_global_data tokens.

    Args:
        None.

    Returns:
        Fresh token dict (at/...) when the page mints tokens again; {}
        when the reloaded page still carries no at (the tab needs a
        manual Google re-login).

    Raises:
        VisionBridgeError: on CDP transport failures.
    """
    tabs = [t for t in _cdp_http("/json/list")
            if t.get("type") == "page" and "gemini.google.com" in t.get("url", "")]
    if not tabs:
        tabs = [_open_fresh_gemini_tab()]
    for tab in reversed(tabs):  # newest first
        try:
            ws = _tab_ws(tab, 30)
            try:
                ws.send(json.dumps({"id": 1, "method": "Page.reload",
                                    "params": {"ignoreCache": False}}))
                receive_cdp(ws, 1, time.monotonic() + 10)
            finally:
                try:
                    ws.close(timeout=0)
                except Exception:
                    pass
        except RequestControlError:
            raise
        except Exception as exc:
            log(f"bridge tab reload issue ({exc}); continuing")
    # Wait for WIZ_global_data to settle back into the fresh document.
    deadline = time.monotonic() + _BRIDGE_RELOAD_WAIT
    while time.monotonic() < deadline:
        bridge_sleep(3)
        try:
            tab = _find_gemini_tab()
            tokens = _map_page_tokens(_tab_evaluate(tab, _TOKEN_JS, _BRIDGE_EVAL_TIMEOUT))
            if tokens.get("at"):
                log("bridge tab reloaded; fresh session tokens acquired")
                return tokens
        except RequestControlError:
            raise
        except Exception:
            continue
    log("bridge tab reload did not restore SNlM0e (manual re-login likely)")
    return {}


def _hybrid_byte_cap() -> int:
    """Resolve the global image byte cap for the hybrid bridge chain.

    Args:
        None.

    Returns:
        Positive byte cap from CONFIG["max_image_bytes"] (20 MiB when
        unset or invalid).
    """
    try:
        value = int(CONFIG.get("max_image_bytes") or 20 * 1024 * 1024)
    except (TypeError, ValueError):
        value = 20 * 1024 * 1024
    return value


@request_scoped
def _server_upload_all(prepared: list) -> list:
    """Upload every prepared image server-side for the hybrid chain.

    Uses the shared browser session with the push_id borrowed from the
    CDP page (via the cached page-token merge), so references land in
    the signed-in account's bucket exactly like the in-page upload. The
    shared reference cache in server.images dedupes multi-turn resends.

    Args:
        prepared: list of (image_bytes, mime_type) tuples.

    Returns:
        List of (ref_string, mime_type) tuples on full success, or None
        when any image cannot be uploaded (caller falls back to the
        in-page base64 chain).
    """
    from .server.images import _upload_one
    check_budget('vision uploads')
    if not prepared:
        return []
    try:
        if len(prepared) > 1:
            return parallel_uploads(prepared, _upload_one)
        return [(str(_upload_one(*prepared[0])), prepared[0][1])]
    except RequestControlError:
        raise
    except Exception as e:
        check_budget('vision uploads')
        log(f'server-side upload failed ({e}); falling back to in-page upload')
        return None


@request_scoped
def vision_generate(prompt: str, images: list,
                    model_id: Optional[int] = None, think_mode: Optional[int] = None) -> str:
    """Generate a completion with images through the logged-in browser tab.

    Args:
        prompt: user prompt text.
        images: list of (image_bytes, mime_type) tuples; http(s) URL
            strings are downloaded first.
        model_id: optional MODE_CATEGORY id forwarded to the page payload.
        think_mode: optional thinking level forwarded to the page payload.

    Returns:
        Raw StreamGenerate response text from the page.

    Raises:
        VisionBridgeError: on bridge, session or upstream failure.
    """
    from .image_fetch import fetch_image_bytes
    from .image_prep import prepare_image

    prepared = []
    for data, mime in images:
        check_budget("vision image preparation")
        if isinstance(data, str):  # defensive: URL entries normalize first
            data = fetch_image_bytes(data)
            mime = None
        if not data:
            raise VisionBridgeError("image fetch failed")
        # Hybrid budget: the global cap; only the in-page base64 fallback
        # below re-fits to MAX_BRIDGE_IMAGE_BYTES.
        data, mime = prepare_image(data, mime or "image/png", _hybrid_byte_cap())
        if len(data) > _hybrid_byte_cap():
            raise VisionBridgeError(
                f"image exceeds limit of {_hybrid_byte_cap()} bytes")
        prepared.append((data, mime))

    queue_end = time.monotonic() + _BRIDGE_CHAIN_WAIT
    while True:
        check_budget('vision bridge queue')
        left = queue_end - time.monotonic()
        if left <= 0:
            raise VisionBridgeError(f'vision bridge busy: queued longer than {_BRIDGE_CHAIN_WAIT:.0f}s')
        if _bridge_lock.acquire(timeout=min(0.1, remaining_timeout(left, 'vision bridge queue'))):
            break
    try:
        check_budget("vision bridge acquired")
        chain_images = prepared
        if CONFIG.get("vision_bridge_server_upload") is not False:
            refs = _server_upload_all(prepared)
            if refs:
                chain_images = refs  # hybrid: page chain skips uploads
            else:
                # In-page fallback carries base64 through the CDP evaluate
                # payload; re-fit anything the looser hybrid budget allowed.
                chain_images = []
                for data, mime in prepared:
                    check_budget("vision fallback preparation")
                    data, mime = prepare_image(data, mime,
                                               MAX_BRIDGE_IMAGE_BYTES)
                    if len(data) > MAX_BRIDGE_IMAGE_BYTES:
                        raise VisionBridgeError(
                            "image exceeds bridge limit of "
                            f"{MAX_BRIDGE_IMAGE_BYTES} bytes")
                    chain_images.append((data, mime))
        js = _page_chain_js(prompt, chain_images, model_id, think_mode)
        data = _run_page_chain(js)
        check_budget("vision generation completion")
        verdict = _classify_chain_failure(data) if data.get("err") else None
        if verdict == "stale_session":
            log(f"bridge chain stale session ({data.get('err', '')[:60]}); "
                "reloading tab and retrying once")
            if _reload_gemini_tab().get("at"):
                check_budget("vision reload retry")
                data = _run_page_chain(js)
                check_budget("vision reload completion")
        elif verdict == "transient":
            log(f"bridge chain transient failure ({data.get('err', '')[:60]}); retrying once")
            check_budget("vision transient retry")
            data = _run_page_chain(js)
            check_budget("vision transient completion")
        if data.get("err") and data.get("stage") == "process_file":
            # Registration keeps rejecting (e.g. upstream pipeline change):
            # one attempt with bare references - the form the reference
            # clients send (HanaokaYuzu/Gemini-API, g4f) when ProcessFile
            # does not exist in their protocol at all.
            log("ProcessFile unusable; retrying with bare references (no UUID)")
            data = _run_page_chain(
                _page_chain_js(prompt, chain_images, model_id, think_mode,
                               allow_bare_ref=True))
        check_budget("vision fallback completion")
        err = data.get("err")
        if err:
            raise VisionBridgeError(f"bridge chain: {err}")
        sg = data.get("sg")
        if not sg:
            raise VisionBridgeError("bridge chain returned no generation")
        log("vision bridge chain ok (%d bytes)" % len(sg))
        return sg
    finally:
        _bridge_lock.release()


# ---------------------------------------------------------------------------
# Page-token sourcing: lend the direct chain the browser-only XSRF token.
# ---------------------------------------------------------------------------
# The server-side chain can upload files and call StreamGenerate on its own
# TLS session, but Google embeds the XSRF token (at/SNlM0e) only in pages
# served to the genuinely logged-in browser session (device-bound via
# DBSC for this account). fetch_page_tokens() reads the live tokens from
# the CDP tab so the direct chain can borrow them. It is wedge-tolerant:
# a tab whose renderer stopped answering is replaced with a freshly
# created one and the corpse is closed. Successes and failures are both
# cached so callers never hammer the browser.

_TOKEN_JS = (
    '(function(){var s=document.documentElement.innerHTML;'
    'function f(p){var m=s.match(p);return m?m[1]:null};'
    'return JSON.stringify({'
    'at:f(/"SNlM0e":"([^"]+)"/),'
    'push:f(/"qKIAYe":"([^"]+)"/),'
    'pctx:f(/"Ylro7b":"([^"]+)"/),'
    'fsid:f(/"FdrFJe":"([^"]+)"/),'
    'bl:f(/"cfb2h":"([^"]+)"/)})})()'
)

_BRIDGE_TOKEN_TTL = 300.0       # success: seconds borrowed tokens are reused
_BRIDGE_TOKEN_FAIL_TTL = 120.0  # failure: cooldown before touching CDP again
_BRIDGE_EVAL_TIMEOUT = 20.0     # per-tab evaluate; wedged tabs exceed this

_bridge_token_state = {"tokens": None, "ts": 0.0, "fail_ts": 0.0}
_bridge_token_lock = threading.Lock()


def _tab_ws(tab: dict, timeout: float):
    """Open a DevTools websocket to one tab with the bridge host rewritten.

    Args:
        tab: target descriptor from /json/list.
        timeout: websocket timeout in seconds.

    Returns:
        Connected websocket client.

    Raises:
        VisionBridgeError: when the connection cannot be established.
    """
    from urllib.parse import urlparse

    import websocket
    _, host, port = _bridge_endpoint()
    ws_url = tab["webSocketDebuggerUrl"]
    wp = urlparse(ws_url)
    ws_url = ws_url.replace(f"{wp.hostname}:{wp.port or 9222}", f"{host}:{port}", 1)
    return websocket.create_connection(ws_url, timeout=remaining_timeout(timeout, "vision CDP connect"), suppress_origin=True)


def _tab_evaluate(tab: dict, js: str, timeout: float):
    """Run one Runtime.evaluate on a tab and return its JSON value.

    Args:
        tab: target descriptor.
        js: expression to evaluate.
        timeout: total wait; exceeding it means the tab is wedged.

    Returns:
        Parsed value, or None when the expression returned nothing.

    Raises:
        Exception: transport/timeout errors (caller treats as wedge).
    """
    deadline = time.monotonic() + remaining_timeout(timeout, 'vision evaluate')
    ws = _tab_ws(tab, remaining_timeout(timeout, 'vision evaluate'))
    try:
        ws.settimeout(remaining_timeout(max(0.001, deadline - time.monotonic()), 'vision evaluate send'))
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                            'params': {'expression': js, 'awaitPromise': True,
                                       'returnByValue': True}}))
        msg = receive_cdp(ws, 1, deadline)
        result = msg.get('result', {})
        if 'exceptionDetails' in result or 'error' in msg:
            raise VisionBridgeError('evaluate raised in page')
        return result.get('result', {}).get('value')
    finally:
        try:
            ws.close(timeout=0)
        except Exception:
            pass


def _close_tabs(target_ids: list) -> None:
    """Close tabs by target id through the browser-level DevTools socket.

    Args:
        target_ids: targetId strings to close; failures are ignored.

    Returns:
        None.
    """
    if not target_ids:
        return
    try:
        ver = _cdp_http("/json/version")
        ws = _tab_ws({"webSocketDebuggerUrl": ver["webSocketDebuggerUrl"]}, 15)
    except RequestControlError:
        raise
    except Exception:
        return
    try:
        for i, tid in enumerate(target_ids):
            ws.send(json.dumps({"id": i + 1, "method": "Target.closeTarget",
                                "params": {"targetId": tid}}))
            bridge_sleep(0.2)
    except RequestControlError:
        raise
    except Exception:
        pass
    finally:
        try:
            ws.close(timeout=0)
        except Exception:
            pass


@request_scoped
def _open_fresh_gemini_tab() -> dict:
    """Create a new tab and navigate it to the Gemini app page.

    Args:
        None.

    Returns:
        The new tab descriptor once /json/list reports it on the app URL.

    Raises:
        VisionBridgeError: when creation or navigation does not settle.
    """
    ver = _cdp_http("/json/version")
    ws = _tab_ws({"webSocketDebuggerUrl": ver["webSocketDebuggerUrl"]}, 30)
    target_id = None
    try:
        ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                            "params": {"url": "about:blank"}}))
        msg = receive_cdp(ws, 1, time.monotonic() + 20)
        target_id = msg.get("result", {}).get("targetId")
        if not target_id:
            raise VisionBridgeError("Target.createTarget returned no id")
    finally:
        try:
            ws.close(timeout=0)
        except Exception:
            pass
    for tab in _cdp_http("/json/list"):
        if tab.get("id") == target_id and tab.get("type") == "page":
            _tab_evaluate(tab,
                          "location.href.indexOf('gemini.google.com') < 0"
                          " && location.assign('https://gemini.google.com/app')",
                          _BRIDGE_EVAL_TIMEOUT)
            break
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        for tab in _cdp_http("/json/list"):
            if (tab.get("id") == target_id and tab.get("type") == "page"
                    and "gemini.google.com" in tab.get("url", "")):
                bridge_sleep(3)  # let WIZ_global_data settle
                return tab
        bridge_sleep(1)
    raise VisionBridgeError("fresh Gemini tab did not navigate")


def _map_page_tokens(raw) -> dict:
    """Validate and map one _TOKEN_JS evaluate result to token names.

    Args:
        raw: Runtime.evaluate return value (expected JSON string).

    Returns:
        Mapped dict with truthy at/push_id/pctx/f_sid/bl values only;
        empty dict when raw is not a JSON object string.
    """
    if not (isinstance(raw, str) and raw.startswith("{")):
        return {}
    data = json.loads(raw)
    tokens = {"at": data.get("at"), "push_id": data.get("push"),
              "pctx": data.get("pctx"), "f_sid": data.get("fsid"),
              "bl": data.get("bl")}
    return {k: v for k, v in tokens.items() if v}


def _fetch_page_tokens_now() -> dict:
    """Read live page tokens from the browser, replacing wedged tabs.

    Args:
        None.

    Returns:
        Dict with at/push_id/pctx/f_sid/bl (mapped names); values missing
        from the page (e.g. at when logged out) are absent. Transport
        failures raise VisionBridgeError.
    """
    wedged = []
    tabs = [t for t in _cdp_http("/json/list")
            if t.get("type") == "page" and "gemini.google.com" in t.get("url", "")]
    for tab in reversed(tabs):  # newest first: old tabs are wedged corpses
        try:
            raw = _tab_evaluate(tab, _TOKEN_JS, _BRIDGE_EVAL_TIMEOUT)
        except RequestControlError:
            raise
        except Exception:
            wedged.append(tab.get("id"))
            continue
        tokens = _map_page_tokens(raw)
        if tokens:
            if wedged:
                _close_tabs(wedged)
            return tokens
        wedged.append(tab.get("id"))
    if wedged:
        _close_tabs(wedged)
    return _map_page_tokens(
        _tab_evaluate(_open_fresh_gemini_tab(), _TOKEN_JS, _BRIDGE_EVAL_TIMEOUT))


@request_scoped
def fetch_page_tokens(force: bool = False) -> dict:
    """Return cached-or-fresh Gemini page tokens from the CDP browser.

    Args:
        force: bypass the caches and read the live page once.

    Returns:
        Token dict (at/push_id/pctx/f_sid/bl subset). Empty dict when the
        browser is unreachable or its tab is not logged in (no at); both
        outcomes cool down for _BRIDGE_TOKEN_FAIL_TTL seconds.
    """
    if not vision_bridge_enabled():
        return {}
    with budget_lock(_bridge_token_lock, "vision token cache"):
        now = time.monotonic()
        if not force:
            if (_bridge_token_state["tokens"]
                    and now - _bridge_token_state["ts"] < _BRIDGE_TOKEN_TTL):
                return dict(_bridge_token_state["tokens"])
            if (not _bridge_token_state["tokens"]
                    and now - _bridge_token_state["fail_ts"] < _BRIDGE_TOKEN_FAIL_TTL):
                return {}
        try:
            tokens = _fetch_page_tokens_now()
        except RequestControlError:
            raise
        except Exception as e:
            check_budget('vision token fetch')
            log(f'bridge token fetch failed: {e}')
            tokens = {}
        if tokens.get("at"):
            _bridge_token_state.update(tokens=tokens, ts=time.monotonic(),
                                       fail_ts=0.0)
        else:
            _bridge_token_state.update(tokens=None, fail_ts=time.monotonic())
        return dict(tokens)
