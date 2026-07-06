"""Tests for the email-OTP login flow."""

import re

import pytest

from app import auth, storage


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()

    # Capture the emailed code instead of sending it.
    sent: dict = {}

    def fake_send(to, subject, html):
        sent["to"] = to
        sent["code"] = re.search(r"(\d{6})", html).group(1)

    monkeypatch.setattr(auth.mailer, "send_email", fake_send)
    return sent


def test_happy_path_returns_session_token(env):
    auth.send_login_code("user@example.com")
    token = auth.verify_login_code("user@example.com", env["code"])
    assert token
    user = storage.get_session_user(token)
    assert user is not None and user.email == "user@example.com"


def test_wrong_code_fails_and_counts_attempts(env):
    auth.send_login_code("user@example.com")
    assert auth.verify_login_code("user@example.com", "000000") is None
    row = storage.get_active_login_code("user@example.com")
    assert row["attempts"] == 1


def test_attempts_are_capped(env):
    auth.send_login_code("user@example.com")
    for _ in range(auth.MAX_VERIFY_ATTEMPTS):
        auth.verify_login_code("user@example.com", "000000")
    # Even the *correct* code is rejected once the cap is hit.
    assert auth.verify_login_code("user@example.com", env["code"]) is None


def test_expired_code_is_rejected(env, monkeypatch):
    auth.send_login_code("user@example.com")
    # Force expiry by advancing "now" past the TTL.
    real_now = auth._utcnow()
    monkeypatch.setattr(auth, "_utcnow", lambda: real_now + auth.CODE_TTL + auth.RESEND_COOLDOWN)
    assert auth.verify_login_code("user@example.com", env["code"]) is None


def test_resend_within_cooldown_keeps_first_code(env):
    auth.send_login_code("user@example.com")
    first_code = env["code"]
    env["code"] = None
    auth.send_login_code("user@example.com")  # within cooldown -> no new send
    assert env["code"] is None  # fake_send not called again
    # The first code still works.
    assert auth.verify_login_code("user@example.com", first_code)


def test_logout_invalidates_session(env):
    auth.send_login_code("user@example.com")
    token = auth.verify_login_code("user@example.com", env["code"])
    assert storage.get_session_user(token) is not None
    auth.logout(token)
    assert storage.get_session_user(token) is None
