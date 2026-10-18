from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.ratelimit import RateLimiter
from app.auth.service import AuthError, AuthService
from app.storage.app_db import AppDB


@pytest.fixture()
def auth(tmp_path: Path) -> AuthService:
    return AuthService(AppDB(tmp_path / "app.db"), "k" * 48, session_ttl_hours=1, password_min_length=10)


def test_password_hashing_roundtrip() -> None:
    h = hash_password("CorrectHorse9")
    assert h.startswith("scrypt$") and "CorrectHorse9" not in h
    assert verify_password("CorrectHorse9", h)
    assert not verify_password("correcthorse9", h)
    assert not verify_password("x", "garbage")
    assert hash_password("same-Password1") != hash_password("same-Password1")  # salted
    assert not needs_rehash(h)


def test_signup_signin_signout(auth: AuthService) -> None:
    s = auth.sign_up("  Anurag@Example.COM ", "Anurag", "CorrectHorse9")
    assert s.user.email == "anurag@example.com"
    assert auth.resolve(s.token).user.id == s.user.id
    s2 = auth.sign_in("anurag@example.com", "CorrectHorse9")
    assert s2.token != s.token
    auth.sign_out(s.token)
    assert auth.resolve(s.token) is None
    assert auth.resolve(s2.token) is not None


@pytest.mark.parametrize(
    ("email", "password", "code"),
    [
        ("not-an-email", "CorrectHorse9", "INVALID_EMAIL"),
        ("a@b.co", "short1", "WEAK_PASSWORD"),
        ("a@b.co", "alllowercaseletters", "WEAK_PASSWORD"),
        ("a@b.co", "x" * 129 + "A1", "WEAK_PASSWORD"),
    ],
)
def test_signup_validation(auth: AuthService, email: str, password: str, code: str) -> None:
    with pytest.raises(AuthError) as info:
        auth.sign_up(email, "Name", password)
    assert info.value.code == code


def test_duplicate_email(auth: AuthService) -> None:
    auth.sign_up("a@b.co", "A", "CorrectHorse9")
    with pytest.raises(AuthError) as info:
        auth.sign_up("A@B.CO", "B", "CorrectHorse9")
    assert info.value.code == "EMAIL_TAKEN"


def test_wrong_password_and_unknown_user_look_identical(auth: AuthService) -> None:
    auth.sign_up("a@b.co", "A", "CorrectHorse9")
    errors = []
    for email in ("a@b.co", "nobody@b.co"):
        with pytest.raises(AuthError) as info:
            auth.sign_in(email, "WrongHorse99")
        errors.append((info.value.code, info.value.message, info.value.status))
    assert errors[0] == errors[1]


def test_session_tokens_are_stored_hashed(auth: AuthService, tmp_path: Path) -> None:
    s = auth.sign_up("a@b.co", "A", "CorrectHorse9")
    rows = sqlite3.connect(tmp_path / "app.db").execute("SELECT token_hash FROM sessions").fetchall()
    assert rows and all(s.token not in r[0] for r in rows)
    assert auth.resolve("forged-token") is None and auth.resolve("x" * 500) is None


def test_csrf_check(auth: AuthService) -> None:
    s = auth.sign_up("a@b.co", "A", "CorrectHorse9")
    session = auth.resolve(s.token)
    assert auth.csrf_ok(session, s.csrf_token)
    assert not auth.csrf_ok(session, None)
    assert not auth.csrf_ok(session, "wrong")


def test_expired_session_rejected(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "app.db")
    svc = AuthService(db, "k" * 48, 1, 10)
    s = svc.sign_up("a@b.co", "A", "CorrectHorse9")
    db.create_session(svc.token_hash("old"), s.user.id, "csrf", ttl_s=-10)
    assert svc.resolve("old") is None


def test_rate_limiter() -> None:
    rl = RateLimiter(limit=3, window_s=60)
    assert [rl.allow("k") for _ in range(4)] == [True, True, True, False]
    assert rl.allow("other")
    rl.reset("k")
    assert rl.allow("k")
