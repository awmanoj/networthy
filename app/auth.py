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
        subject=f"{code} is your Networthy login code",
        html=_login_code_email_html(code),
    )


def _login_code_email_html(code: str) -> str:
    """A branded, client-robust HTML email for the login code.

    Table-based layout with inline styles so it renders consistently across
    email clients (Gmail, Outlook, Apple Mail), which strip <style>/external CSS.
    """
    minutes = int(CODE_TTL.total_seconds() // 60)
    return f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f7f8fa;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="background:#f7f8fa;padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="max-width:440px;background:#ffffff;border:1px solid #e2e5ea;
                        border-radius:12px;overflow:hidden;
                        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
            <tr>
              <td style="padding:24px 28px 8px;">
                <div style="font-size:18px;font-weight:700;letter-spacing:-0.02em;color:#1a1d24;">
                  Networthy
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 28px 0;">
                <p style="margin:0;font-size:15px;line-height:1.5;color:#1a1d24;">
                  Use this code to sign in:
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 28px;">
                <div style="background:#f0f2f5;border:1px solid #e2e5ea;border-radius:10px;
                            padding:18px 0;text-align:center;">
                  <span style="font-family:'SFMono-Regular',Menlo,Consolas,monospace;
                               font-size:32px;font-weight:700;letter-spacing:8px;color:#1a1d24;">
                    {code}
                  </span>
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:0 28px 24px;">
                <p style="margin:0;font-size:13px;line-height:1.5;color:#6b7280;">
                  This code expires in {minutes} minutes. If you didn't request it,
                  you can safely ignore this email.
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 28px;background:#f7f8fa;border-top:1px solid #e2e5ea;">
                <p style="margin:0;font-size:12px;line-height:1.5;color:#8b93a3;">
                  Networthy · Your data is private to your account.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


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
