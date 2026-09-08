"""Gemini Web wire protocol: request headers, f.req payload, endpoints."""
import hashlib
import json
import time
import urllib.parse
import uuid

from ..config import CONFIG
from .cookies import _active_auth_user, get_active_xsrf_token, load_cookie
from .transport import CHROME_UA


def make_sapisidhash(sapisid: str) -> str:
    """Build the timestamped Gemini SAPISID authorization header.

    Args:
        sapisid: SAPISID cookie value for the active account.

    Returns:
        Formatted SAPISIDHASH header value.
    """
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Resolve the selected cookie before choosing a Gemini account prefix.

    Args:
        None.

    Returns:
        Account path prefix, or an empty string for the default account.
    """
    auth_user = _active_auth_user()
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(uuid_val: str = None) -> dict:
    """Build request headers for StreamGenerate.

    Args:
        uuid_val: request uuid shared with inner[59] of the payload. When
            set, it is also sent as the x-goog-ext-525005358-jspb header
            (["<uuid>",1]) like the current web client, which binds the
            request to uploaded file references.

    Returns:
        Header dict for the StreamGenerate POST.
    """
    cookie_str, sapisid = load_cookie()
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": CHROME_UA,
    }
    if uuid_val:
        headers["x-goog-ext-525005358-jspb"] = f'["{uuid_val}",1]'
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(_active_auth_user())
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload.

    Args:
        inner: Mutable Gemini request payload slots.

    Returns:
        None.
    """
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, uuid_val: str = None) -> str:
    """Build the urlencoded f.req payload for StreamGenerate.

    Args:
        prompt: user prompt text.
        model_id: MODE_CATEGORY id (1=FAST, 2=THINKING, 3=PRO, 4=AUTO...).
        think_mode: thinking level (0=dynamic, 4=default).
        file_refs: list of uploaded file references to attach.
        extra_fields: optional {index: value} overrides for inner payload slots.
        uuid_val: request uuid for inner[59]; generated (uppercase) when omitted.

    Returns:
        urlencoded request body string.
    """
    inner = [None] * 102
    if file_refs:
        # Attachment tuples copied verbatim from browser captures (see
        # zexadev/gemini-web2api-go): each entry is
        #   [[ref, kind, null, mime], filename, null x6, [0]]
        # kind: 1=image, 2=video, 3=text. Shorter shapes (e.g. [[ref], name])
        # upload fine but the generate call is rejected with
        # BardErrorInfo [1100].
        refs = []
        for ref in file_refs:
            mime_type = getattr(ref, "mime_type", "image/png") or "image/png"
            refs.append([[str(ref), 1, None, mime_type], "image.png",
                         None, None, None, None, None, None, [0]])
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = uuid_val or str(uuid.uuid4()).upper()
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    xsrf_token = get_active_xsrf_token()
    if xsrf_token:
        params["at"] = xsrf_token
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    """Construct the generation endpoint for the resolved active account.

    Args:
        None.

    Returns:
        StreamGenerate URL with build and request-id parameters.
    """
    reqid = int(time.time() * 1000) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def _delete_url() -> str:
    """Construct the history endpoint for the resolved active account.

    Args:
        None.

    Returns:
        Batchexecute URL with account, RPC, build and request-id fields.
    """
    reqid = int(time.time() * 1000) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "batchexecute"
        f"?rpcids=hNktQb&bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
