"""gemini-web2api: Gemini Web to OpenAI-compatible API proxy.

Package layout:
    config / logs / models     - service configuration and catalog
    upstream/                  - Gemini Web protocol client (transport,
                                 cookies, wire protocol, parsing, retries)
    keepalive / multimodal     - session self-renewal and image uploads
    tools / batching           - OpenAI tool-call parsing, request batching
    server/                    - HTTP API layer (OpenAI + Google protocols)
"""
__version__ = "1.2.1"
