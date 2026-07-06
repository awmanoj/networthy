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
    return os.environ.get("EMAIL_FROM", "Networthy <onboarding@resend.dev>")


def send_email(to: str, subject: str, html: str) -> None:
    """Send an email, or log it in dev when no provider is configured."""
    key = _api_key()
    if not key:
        logger.warning(
            "EMAIL (dev fallback, not actually sent)\n  to=%s\n  subject=%s\n  %s",
            to,
            subject,
            html,
        )
        return

    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"from": _from_address(), "to": [to], "subject": subject, "html": html},
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        # Don't leak provider errors to the user; the login flow shows a generic
        # "code sent" screen regardless. Log for the operator.
        logger.exception("Failed to send email to %s", to)
