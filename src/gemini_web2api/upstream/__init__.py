"""Upstream layer: the Gemini Web protocol client.

Public API re-exported here so callers depend on the package, not on the
internal module layout:

    generate / generate_stream / start_keep_warm   - generation pipeline
    pick_next_cookie / load_cookie                 - cookie pool
    make_sapisidhash / _build_payload / _build_headers - wire protocol
    extract_response_text / extract_conversation_id    - response parsing
    transport selection (HAS_HTTPX / HAS_CURL_CFFI / get_browser_session)
"""
from .cookies import (_active_cookie_path, _cookie_caches, load_cookie,
                      pick_next_cookie)
from .generate import generate, generate_stream, start_keep_warm
from .history import delete_conversation, schedule_history_delete
from .parser import (clean_text, extract_conversation_id,
                     extract_response_text)
from .protocol import _build_headers, _build_payload, make_sapisidhash
from .transport import CHROME_UA, HAS_CURL_CFFI, HAS_HTTPX, get_browser_session

__all__ = [
    "CHROME_UA", "HAS_CURL_CFFI", "HAS_HTTPX",
    "_active_cookie_path", "_build_headers", "_build_payload", "_cookie_caches",
    "clean_text", "delete_conversation", "extract_conversation_id",
    "extract_response_text", "generate", "generate_stream", "get_browser_session",
    "load_cookie", "make_sapisidhash", "pick_next_cookie",
    "schedule_history_delete", "start_keep_warm",
]
