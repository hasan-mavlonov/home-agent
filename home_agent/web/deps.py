"""Request dependencies: authentication, CSRF, and access to services."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from ..security.auth import SESSION_COOKIE, Session

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def services(request: Request):
    return request.app.state.services


def auth_service(request: Request):
    return request.app.state.auth


def client_ip(request: Request) -> str:
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if host in ("127.0.0.1", "::1") and forwarded:
        return forwarded.split(",")[0].strip()[:100]
    return host


def require_session(request: Request) -> Session:
    """Every /api route except login/session/health goes through this.

    State-changing requests also need the per-session CSRF token in X-CSRF-Token,
    which a cross-site page cannot read, and must not be marked cross-site by the browser.
    """
    session = request.app.state.auth.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    if request.method in UNSAFE_METHODS:
        if request.headers.get("sec-fetch-site", "") == "cross-site":
            raise HTTPException(status_code=403, detail="Cross-site request blocked")
        sent = request.headers.get("x-csrf-token", "")
        if not sent or not secrets.compare_digest(sent, session.csrf_token):
            raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
    request.state.session = session
    return session
