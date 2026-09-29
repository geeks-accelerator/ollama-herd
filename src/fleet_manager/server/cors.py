"""Opt-in CORS for browser-based clients — Herd's equivalent of ``OLLAMA_ORIGINS``.

Browser clients (Hollama, TypingMind, Chatbox web, Page Assist) call the router
straight from a web page, so the browser demands CORS headers and a preflight
``OPTIONS`` answer before it will hand them a response.  Herd sent neither, so
every one of those clients failed against it while working against Ollama.

The allow-list comes from ``FLEET_CORS_ORIGINS`` and is **empty by default**:
no middleware is installed at all, so an existing router's behaviour is byte-
for-byte what it was.  Syntax follows ``OLLAMA_ORIGINS``: comma-separated
origins, ``*`` inside an origin is a wildcard (``http://localhost:*``,
``chrome-extension://*``), and a bare ``*`` allows any origin.

One deliberate difference from Ollama: Ollama always allows a built-in set of
localhost/app origins on top of the user's list.  Herd adds nothing implicitly —
the router listens on 0.0.0.0 for the whole LAN, so the operator names every
origin it trusts.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

# Response headers a browser client is allowed to read.  CORS hides every
# non-safelisted header by default, which would make Herd's routing metadata
# invisible to exactly the clients this exists for.
EXPOSED_HEADERS = [
    "X-Fleet-Node",
    "X-Fleet-Served-Model",
    "X-Fleet-Requested-Model",
    "X-Fleet-Fallback",
    "X-Fleet-Backend",
    "X-Fleet-Retries",
    "X-Fleet-Score",
    "X-Fleet-Affinity",
    "X-Fleet-Context-Overflow",
    "X-Thinking-Tokens",
    "X-Output-Tokens",
    "X-Done-Reason",
    "X-Budget-Used",
    "Retry-After",
]

# Same verbs Ollama's CORS config allows.
ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def parse_cors_origins(raw: str) -> tuple[list[str], str | None]:
    """Split an ``OLLAMA_ORIGINS``-style list into Starlette's two knobs.

    Returns ``(allow_origins, allow_origin_regex)``: exact origins go in the
    list, wildcard patterns are compiled into one anchored regex (Starlette
    ``fullmatch``es it).  A bare ``*`` short-circuits to ``(["*"], None)``.
    Empty input → ``([], None)``, meaning "CORS off".
    """
    entries = [e.strip().rstrip("/") for e in (raw or "").split(",")]
    entries = [e for e in entries if e]
    if "*" in entries:
        return ["*"], None
    exact = [e for e in entries if "*" not in e]
    patterns = [re.escape(e).replace(r"\*", ".*") for e in entries if "*" in e]
    regex = "|".join(f"(?:{p})" for p in patterns) if patterns else None
    return exact, regex


def install_cors(app: FastAPI, raw_origins: str) -> bool:
    """Add ``CORSMiddleware`` when an allow-list is configured.

    Returns True when installed.  With no origins configured nothing is added,
    so the default router emits no CORS headers and ``OPTIONS`` stays 405 —
    the pre-existing security posture.
    """
    allow_origins, allow_origin_regex = parse_cors_origins(raw_origins)
    if not allow_origins and not allow_origin_regex:
        return False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_origin_regex=allow_origin_regex,
        allow_methods=ALLOWED_METHODS,
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
        # Ollama doesn't allow credentials either, and Herd has no cookies to
        # protect; keeping it off also lets a bare "*" be a literal wildcard.
        allow_credentials=False,
    )
    return True
