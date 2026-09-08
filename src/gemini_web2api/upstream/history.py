"""Conversation history deletion (experimental, best-effort)."""
import json
import threading
import urllib.parse

from ..config import CONFIG
from ..logs import log
from .cookies import (
    _active_auth_user, _active_cookie_path, restore_active_cookie,
    get_active_xsrf_token, set_active_auth_user, set_active_cookie,
)
from .protocol import _build_headers, _delete_url
from .transport import HAS_HTTPX, _get_httpx_client, _urllib_post


def delete_conversation(cid: str, cookie_path: str = None, auth_user=None) -> bool:
    """EXPERIMENTAL: best-effort delete of a conversation from account history.

    Upstream rejects the hNktQb batchexecute call on current builds (XSRF
    error 138/139), so this only logs failures. Recommended alternative:
    temporary_chats=true (conversations are never saved at all).

    Args:
        cid: Conversation id returned by the originating account.
        cookie_path: Originating cookie file, or None for the active account.
        auth_user: Originating account index override when provided.

    Returns:
        True on apparent success; False for missing ids or upstream failures.
    """
    if not cid:
        return False
    scoped = cookie_path is not None or auth_user is not None
    previous = set_active_cookie(cookie_path or _active_cookie_path()) if scoped else None
    try:
        if auth_user is not None:
            set_active_auth_user(auth_user)
        return _delete_conversation_active(cid)
    finally:
        if scoped:
            restore_active_cookie(previous)


def _delete_conversation_active(cid: str) -> bool:
    """Delete a conversation using the account already bound to the thread.

    Args:
        cid: Conversation id returned by StreamGenerate.

    Returns:
        True when the upstream did not report a rejection.
    """
    inner_arg = '["' + cid + '",1]'
    payload = json.dumps([[["hNktQb", inner_arg, None, "generic"]]], separators=(",", ":"))
    body = urllib.parse.urlencode({"f.req": payload, "at": get_active_xsrf_token() or ""})
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
    """Fire and forget deletion while preserving the originating account.

    Args:
        cid: Conversation id to delete.

    Returns:
        None.
    """
    if not CONFIG.get("auto_delete_history") or not cid:
        return
    if CONFIG.get("temporary_chats"):
        # Temporary chats are never saved server-side; the delete RPC would
        # just 400 (noise + a wasted upstream request on every generation).
        return
    cookie_path = _active_cookie_path()
    auth_user = _active_auth_user()
    threading.Thread(
        target=delete_conversation, args=(cid, cookie_path, auth_user),
        daemon=True, name="history-delete",
    ).start()
