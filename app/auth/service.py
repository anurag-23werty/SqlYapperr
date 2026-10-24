"""Sign-up, sign-in, sessions and CSRF.

Session design:
* The browser holds a random 256-bit token in an ``HttpOnly`` cookie.
* The server stores only ``HMAC-SHA256(secret_key, token)``, so a leaked app
  database does not yield usable session cookies.
* Every session has its own CSRF token. State-changing requests must echo it
  in the ``X-CSRF-Token`` header (double-submit, verified server-side).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from app.storage.app_db import AppDB, DuplicateEmailError, SessionRecord, User
import unicodedata
import uuid
from dataclasses import dataclass

from app.auth.passwords import DUMMY_HASH, hash_password, needs_rehash, verify_password
from app.storage.app_db import AppDB, SessionRecord, User

SESSION_COOKIE = "sqly_session"
CSRF_COOKIE = "sqly_csrf"
CSRF_HEADER = "x-csrf-token"

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    user: User


def normalize_email(email: str) -> str:
    return unicodedata.normalize("NFKC", email).strip().lower()


def validate_password(password: str, min_length: int) -> None:
    if len(password) < min_length:
        raise AuthError("WEAK_PASSWORD", f"password must be at least {min_length} characters")
    if len(password) > 128:
        raise AuthError("WEAK_PASSWORD", "password must be at most 128 characters")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 2:
        raise AuthError("WEAK_PASSWORD", "use at least two of: lowercase, uppercase, digits, symbols")


class AuthService:
    def __init__(self, db: AppDB, secret_key: str, session_ttl_hours: int, password_min_length: int) -> None:
        self.db = db
        self._key = secret_key.encode("utf-8")
        self._ttl_s = session_ttl_hours * 3600
        self._min_len = password_min_length

    def token_hash(self, token: str) -> str:
        return hmac.new(self._key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    # ----------------------------------------------------------- public API
    def sign_up(self, email: str, name: str, password: str) -> IssuedSession:
        email = normalize_email(email)
        name = " ".join(unicodedata.normalize("NFKC", name).split())[:80]
        if not _EMAIL.fullmatch(email):
            raise AuthError("INVALID_EMAIL", "enter a valid email address")
        if not name:
            raise AuthError("INVALID_NAME", "name is required")
        validate_password(password, self._min_len)
        try:
            user = self.db.create_user(uuid.uuid4().hex, email, name, hash_password(password))
        except DuplicateEmailError:
            # Same shape as success-path errors elsewhere; does not reveal more than necessary.
            raise AuthError("EMAIL_TAKEN", "an account with this email already exists", 409) from None
        return self._issue(user)

    def sign_in(self, email: str, password: str) -> IssuedSession:
        email = normalize_email(email)
        found = self.db.user_credentials(email)
        if found is None:
            verify_password(password, DUMMY_HASH)  # equalise timing
            raise AuthError("INVALID_CREDENTIALS", "email or password is incorrect", 401)
        user, stored = found
        if not verify_password(password, stored):
            raise AuthError("INVALID_CREDENTIALS", "email or password is incorrect", 401)
        if needs_rehash(stored):
            self.db.update_password_hash(user.id, hash_password(password))
        return self._issue(user)

    def resolve(self, token: str | None) -> SessionRecord | None:
        if not token or len(token) > 200:
            return None
        return self.db.get_session(self.token_hash(token))

    def sign_out(self, token: str | None) -> None:
        if token:
            self.db.delete_session(self.token_hash(token))

    @staticmethod
    def csrf_ok(session: SessionRecord, header_value: str | None) -> bool:
        return bool(header_value) and hmac.compare_digest(session.csrf_token, header_value or "")

    # -------------------------------------------------------------- helpers
    def _issue(self, user: User) -> IssuedSession:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self.db.create_session(self.token_hash(token), user.id, csrf, self._ttl_s)
        return IssuedSession(token=token, csrf_token=csrf, user=user)

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl_s)
