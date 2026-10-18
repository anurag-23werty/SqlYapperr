"""Request dependencies: services, current user, CSRF and same-origin checks."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.auth.service import CSRF_HEADER, SESSION_COOKIE
from app.services import Services
from app.storage.app_db import SessionRecord


def services(request: Request) -> Services:
    return request.app.state.services


def request_id(request: Request) -> str:
    return request.state.request_id


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_session(request: Request) -> SessionRecord:
    session = services(request).auth.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return session


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # non-browser clients; the CSRF token still applies
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc == host


def csrf_session(request: Request) -> SessionRecord:
    """For state-changing requests: valid session + matching CSRF token + same origin."""
    session = current_session(request)
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request blocked")
    if not services(request).auth.csrf_ok(session, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
    return session


def same_origin_only(request: Request) -> None:
    """For sign-in/sign-up (no session yet): block cross-site form posts."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request blocked")
