"""Gemini Web wire protocol: request headers, f.req payload, endpoints."""
import hashlib
import json
import time
import urllib.parse
import uuid

from ..config import CONFIG
from .cookies import _active_auth_user, load_cookie
from .transport import CHROME_UA


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = _active_auth_user()
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(uuid_val: str = None) -> dict:
    """Build request headers for StreamGenerate.

    Parameters:
        uuid_val: request uuid shared with inner[59] of the payload. When
            set, it is also sent as the x-goog-ext-525005358-jspb header
            (["<uuid>",1]) like the current web client, which binds the
            request to uploaded file references.

    Returns:
        Header dict for the StreamGenerate POST.
    """
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
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, uuid_val: str = None) -> str:
    """Build the urlencoded f.req payload for StreamGenerate.

    Parameters:
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
        refs = [
            [[ref, 1, None, "image/png"], "image.png",
             None, None, None, None, None, None, [0]]
            for ref in file_refs
        ]
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
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time() * 1000) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def _delete_url() -> str:
    reqid = int(time.time() * 1000) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "batchexecute"
        f"?rpcids=hNktQb&bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )
