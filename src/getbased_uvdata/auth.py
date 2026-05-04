"""Bearer-token middleware. One token, set via env."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request


def required_bearer() -> str | None:
    """Return the configured bearer or None when auth is disabled."""
    v = os.environ.get("GETBASED_UVDATA_BEARER", "").strip()
    return v or None


def check_bearer(request: Request) -> None:
    """Raise 401 unless the request carries a matching Authorization header.

    No-op when the env bearer is unset — that's the "open public" mode,
    only sensible for hosted instances behind their own access control
    (Cloudflare Access, Vercel auth, etc).
    """
    expected = required_bearer()
    if expected is None:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    presented = header[7:].strip()
    # Constant-time compare so timing differences don't leak the token.
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")
