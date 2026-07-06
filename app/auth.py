"""Email + one-time-code authentication and session handling.

Login flow: user submits an email -> we email a 6-digit code -> user submits the
code -> we create a session and set an opaque cookie. Accounts are created on
first successful login (open signup). Sessions are server-side rows; the cookie
only carries a random token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from . import mailer, storage

# --- Tunables ---------------------------------------------------------------
CODE_TTL = timedelta(minutes=10)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_VERIFY_ATTEMPTS = 5
SESSION_TTL = timedelta(days=30)

SESSION_COOKIE = "session"

# Paths reachable without a session. Everything else redirects to /login.
_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_PATHS = {"/login", "/verify", "/logout", "/health"}

_DB_TIME_FMT = "%Y-%m-%d %H:%M:%S"  # matches SQLite's datetime('now') (UTC)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "true").lower() != "false"


# --- OTP --------------------------------------------------------------------

def _hash_code(email: str, code: str) -> str:
    """Hash a code bound to its email so a DB row never holds the plaintext.

    APP_SECRET (if set) keys the hash so a DB leak alone can't brute-force the
    short numeric code; otherwise falls back to a plain salted digest.
    """
    email = email.strip().lower()
    secret = os.environ.get("APP_SECRET")
    if secret:
        return hmac.new(
            secret.encode(), f"{email}:{code}".encode(), hashlib.sha256
        ).hexdigest()
    return hashlib.sha256(f"{email}:{code}".encode()).hexdigest()


def send_login_code(email: str) -> None:
    """Generate, store, and email a login code — unless one was just sent.

    Silently no-ops within the resend cooldown (the previously emailed code is
    still valid), so the caller can always show the same "code sent" screen
    without leaking whether a fresh mail went out.
    """
    email = email.strip().lower()
    existing = storage.get_active_login_code(email)
    if existing is not None:
        created = datetime.strptime(existing["created_at"], _DB_TIME_FMT)
        if _utcnow() - created < RESEND_COOLDOWN:
            return

    code = f"{secrets.randbelow(1_000_000):06d}"
    storage.create_login_code(email, _hash_code(email, code), _utcnow() + CODE_TTL)

    mailer.send_email(
        to=email,
        subject="Your Networthy login code",
        html=(
            f"<p>Your Networthy login code is:</p>"
            f"<p style='font-size:24px;font-weight:700;letter-spacing:2px'>{code}</p>"
            f"<p>It expires in 10 minutes. If you didn't request this, ignore it.</p>"
        ),
    )


def verify_login_code(email: str, code: str) -> str | None:
    """Verify a submitted code. On success return a new session token, else None."""
    email = email.strip().lower()
    row = storage.get_active_login_code(email)
    if row is None:
        return None
    if datetime.strptime(row["expires_at"], _DB_TIME_FMT) <= _utcnow():
        storage.consume_login_code(email)
        return None
    if row["attempts"] >= MAX_VERIFY_ATTEMPTS:
        return None

    if hmac.compare_digest(row["code_hash"], _hash_code(email, code)):
        storage.consume_login_code(email)
        user = storage.get_or_create_user(email)
        token = secrets.token_urlsafe(32)
        storage.create_session(user.id, token, _utcnow() + SESSION_TTL)
        return token

    storage.increment_code_attempts(email)
    return None


def logout(token: str | None) -> None:
    if token:
        storage.delete_session(token)


# --- Middleware -------------------------------------------------------------

def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


class SessionMiddleware(BaseHTTPMiddleware):
    """Resolve the session cookie to a user; gate non-public routes behind it."""

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(SESSION_COOKIE)
        request.state.user = storage.get_session_user(token) if token else None

        if not _is_public(request.url.path) and request.state.user is None:
            return RedirectResponse(url="/login", status_code=303)

        return await call_next(request)
