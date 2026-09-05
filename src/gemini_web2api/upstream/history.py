"""Conversation history deletion (experimental, best-effort)."""
import json
import threading
import urllib.parse

from ..config import CONFIG
from ..logs import log
from .protocol import _build_headers, _delete_url
from .transport import HAS_HTTPX, _get_httpx_client, _urllib_post


def delete_conversation(cid: str) -> bool:
    """EXPERIMENTAL: best-effort delete of a conversation from account history.

    Upstream rejects the hNktQb batchexecute call on current builds (XSRF
    error 138/139), so this only logs failures. Recommended alternative:
    temporary_chats=true (conversations are never saved at all).
    """
    if not cid:
        return False
    inner_arg = '["' + cid + '",1]'
    payload = json.dumps([[["hNktQb", inner_arg, None, "generic"]]], separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": payload, "at": CONFIG.get("xsrf_token") or ""})
    headers = _build_headers()
    client = _get_httpx_client() if HAS_HTTPX else None
    try:
        if client is not None:
            resp = client.post(_delete_url(), content=body, headers=headers, timeout=15)
            text = resp.text
            resp.raise_for_status()
        else:
            text = _urllib_post(_delete_url(), body.encode(), headers)
        ok = "BardErrorInfo" not in text
        log(f"History delete cid={cid[:14]}...: {'ok' if ok else 'rejected'}")
        return ok
    except Exception as e:
        log(f"History delete failed cid={cid[:14]}...: {e}")
        return False


def schedule_history_delete(cid: str):
    """Fire and forget history deletion (auto_delete_history config)."""
    if not CONFIG.get("auto_delete_history") or not cid:
        return
    if CONFIG.get("temporary_chats"):
        # Temporary chats are never saved server-side; the delete RPC would
        # just 400 (noise + a wasted upstream request on every generation).
        return
    threading.Thread(target=delete_conversation, args=(cid,), daemon=True).start()
