"""Transactional email delivery.

Uses Resend's HTTP API when RESEND_API_KEY is set. Without it (local dev, tests),
falls back to logging the message so the OTP is visible in the server console /
`docker logs` — no external calls, no email account needed to develop.

Named `mailer` rather than `email` to avoid shadowing the stdlib `email` package.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("networthy.mailer")

RESEND_API_URL = "https://api.resend.com/emails"


def _api_key() -> str | None:
    return os.environ.get("RESEND_API_KEY")


def _from_address() -> str:
    # Resend requires a verified sender; default is a placeholder for dev only.
    return os.environ.get("EMAIL_FROM", "Networthy HQ <onboarding@resend.dev>")


def send_email(to: str, subject: str, html: str,
               reply_to: str | None = None) -> bool:
    """Send an email, or log it in dev when no provider is configured.

    Returns True only when a provider actually accepted it. The login flow
    ignores the result by design (it shows "code sent" either way, so a wrong
    address can't be probed), but a feedback form needs to know — telling
    someone their bug report was delivered when it wasn't is worse than saying
    it was recorded.
    """
    key = _api_key()
    if not key:
        logger.warning(
            "EMAIL (dev fallback, not actually sent)\n  to=%s\n  subject=%s\n  %s",
            to,
            subject,
            html,
        )
        return False

    payload = {"from": _from_address(), "to": [to], "subject": subject, "html": html}
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        # Don't leak provider errors to the user; the login flow shows a generic
        # "code sent" screen regardless. Log for the operator.
        logger.exception("Failed to send email to %s", to)
        return False
    return True
