"""CDP vision bridge: run the image chain inside a logged-in Gemini tab.

Google's web pipeline only hands the XSRF token (SNlM0e) to a genuinely
signed-in browser session, so image understanding is executed inside the
Gemini page of a CDP-attached Chrome (the llq desktop seat) that a real
account has logged into. The bridge uploads each image, registers it via
ProcessFile (receiving the attachment UUID) and sends StreamGenerate with
the at token — the exact chain the web client uses.
"""
import base64
import json
import socket
import threading
import urllib.request

from .config import CONFIG
from .logs import log

# One browser tab executes one chain at a time; the page-level tokens are
# session-scoped and concurrent evaluates would interleave.
_bridge_lock = threading.Lock()

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
    base = _bridge_endpoint()[0]
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        raise VisionBridgeError(f"vision bridge unreachable: {exc}") from exc


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


def _page_chain_js(prompt: str, images: list) -> str:
    """Build the in-page chain script (upload -> ProcessFile -> StreamGenerate).

    Args:
        prompt: user prompt text (already JSON-escaped by json.dumps).
        images: list of (image_bytes, mime_type) tuples.

    Returns:
        JavaScript source string for Runtime.evaluate with awaitPromise.
    """
    parts = []
    for data, mime in images:
        b64 = base64.b64encode(data).decode()
        parts.append({"b64": b64, "mime": mime})
    payload = json.dumps({"prompt": prompt, "images": parts})
    return """(async function () {
  var PAYLOAD = %s;
  try {
    function wiz(key) {
      var m = document.documentElement.innerHTML.match(new RegExp('"' + key + '":"([^"]+)"'));
      return m ? m[1] : null;
    }
    var at = wiz('SNlM0e'), fsid = wiz('FdrFJe'), bl = wiz('cfb2h');
    var pushId = wiz('qKIAYe'), pctx = wiz('Ylro7b');
    if (!at || !pushId) return JSON.stringify({err: 'session not ready (no at/push_id; logged in?)'});
    var entries = [];
    for (var i = 0; i < PAYLOAD.images.length; i++) {
      var img = PAYLOAD.images[i];
      var bytes = Uint8Array.from(atob(img.b64), function (c) { return c.charCodeAt(0); });
      var r1 = await fetch('https://push.clients6.google.com/upload/', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                  'Push-ID': pushId, 'X-Client-Pctx': pctx,
                  'X-Goog-Upload-Command': 'start', 'X-Goog-Upload-Protocol': 'resumable',
                  'X-Goog-Upload-Header-Content-Length': String(bytes.length),
                  'X-Tenant-Id': 'bard-storage'},
        body: 'File name: image_' + (i + 1) + '.png'
      });
      var putUrl = r1.headers.get('x-goog-upload-url');
      if (!putUrl) return JSON.stringify({err: 'upload start failed ' + r1.status});
      var r2 = await fetch(putUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
                  'Push-ID': pushId, 'X-Client-Pctx': pctx,
                  'X-Goog-Upload-Command': 'upload, finalize',
                  'X-Goog-Upload-Offset': '0', 'X-Tenant-Id': 'bard-storage'},
        body: bytes
      });
      var ref = (await r2.text()).trim();
      if (ref.indexOf('/contrib') !== 0) return JSON.stringify({err: 'upload failed: ' + ref.slice(0, 80)});
      var pfInner = [[[ref, null, 1, img.mime], 'image_' + (i + 1) + '.png'], null, 1, ['zh-CN']];
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
      if (!um) return JSON.stringify({err: 'ProcessFile failed: ' + pfText.slice(0, 100)});
      entries.push([[ref, 1, null, img.mime, um[0]], 'image_' + (i + 1) + '.png']);
    }
    var inner = new Array(102).fill(null);
    inner[0] = [PAYLOAD.prompt, 0, null, entries, null, null, 0];
    inner[1] = ['zh-CN']; inner[6] = [0]; inner[7] = 1; inner[10] = 1; inner[11] = 0;
    inner[17] = [[0]]; inner[18] = 0; inner[27] = 1; inner[30] = [4]; inner[41] = [2];
    inner[53] = 0; inner[59] = 'BRDG' + Date.now().toString(16).toUpperCase() + '-4A01-4C22-9F61A7E89B01';
    inner[61] = []; inner[68] = 1; inner[79] = 1;
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
      return JSON.stringify({err: 'upstream rejected: ' + (em ? em[1] : 'unknown')});
    }
    return JSON.stringify({sg: sgText});
  } catch (e) {
    return JSON.stringify({err: 'chain exception: ' + String(e).slice(0, 150)});
  }
})()""" % payload


def vision_generate(prompt: str, images: list) -> str:
    """Generate a completion with images through the logged-in browser tab.

    Args:
        prompt: user prompt text.
        images: list of (image_bytes, mime_type) tuples.

    Returns:
        Raw StreamGenerate response text from the page.

    Raises:
        VisionBridgeError: on bridge, session or upstream failure.
    """
    oversized = [b for b, _ in images if len(b) > MAX_BRIDGE_IMAGE_BYTES]
    if oversized:
        raise VisionBridgeError(
            f"image exceeds bridge limit of {MAX_BRIDGE_IMAGE_BYTES} bytes")
    tab = _find_gemini_tab()
    import websocket  # from requirements: websocket-client
    _, host, port = _bridge_endpoint()
    ws_url = tab["webSocketDebuggerUrl"]
    # Rewrite the loopback host Chrome reports to the bridge endpoint.
    from urllib.parse import urlparse
    wp = urlparse(ws_url)
    ws_url = ws_url.replace(f"{wp.hostname}:{wp.port or 9222}", f"{host}:{port}", 1)
    ws = websocket.create_connection(ws_url, timeout=200, suppress_origin=True)
    try:
        js = _page_chain_js(prompt, images)
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": js, "awaitPromise": True,
                                        "returnByValue": True}}))
        raw = None
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                raw = msg
                break
        if raw is None or raw.get("error"):
            raise VisionBridgeError(f"evaluate failed: {raw and raw['error']}")
        result = raw["result"].get("result", {})
        if "exceptionDetails" in raw["result"]:
            raise VisionBridgeError("page exception during vision chain")
        value = result.get("value")
        if not value:
            raise VisionBridgeError("empty bridge result")
        data = json.loads(value)
        if data.get("err"):
            raise VisionBridgeError(f"bridge chain: {data['err']}")
        sg = data.get("sg")
        if not sg:
            raise VisionBridgeError("bridge chain returned no generation")
        log("vision bridge chain ok (%d bytes)" % len(sg))
        return sg
    finally:
        try:
            ws.close()
        except Exception:
            pass
