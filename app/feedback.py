"""Bug reports and feedback — a form that turns into an email.

Deliberately a **form**, not a `mailto:` link. A mailto needs a configured mail
client, which a large share of phone and webmail users simply don't have, and it
hands the reporter an address to stare at instead of a box to type in. The form
also lets the destination stay out of the page: the address it reaches is set by
env on the server, never rendered.

Two properties this has to keep, because a feedback box is the one place where a
privacy-first app invites the user to type free text that leaves the machine:

1. **Nothing is attached that the user didn't type.** No referring URL, no page
   path, no session context. A signed-in URL names which asset classes someone
   holds — `/networth/assets/financial-assets/crypto` — and quietly mailing that
   to the operator is exactly what the Google Analytics gates exist to prevent.
   The one exception is the account email, which is stated on the form, and is
   there so a reply can reach them.
2. **Stored as well as sent.** `mailer.send_email` swallows provider errors and
   no-ops entirely without `RESEND_API_KEY`, so email alone can drop a report
   silently. A bug report nobody ever sees is the failure that matters, so the
   row is written first and the mail is best-effort on top.
"""

from __future__ import annotations

import html
import os

from app import auth, mailer, storage

# What kind of report this is. Coarse on purpose — a long taxonomy makes the
# reporter think about filing rather than about the problem.
KINDS: list[dict] = [
    {"slug": "bug", "label": "Something is broken"},
    {"slug": "wrong-number", "label": "A number looks wrong"},
    {"slug": "idea", "label": "An idea or a request"},
    {"slug": "other", "label": "Something else"},
]
KIND_BY_SLUG = {k["slug"]: k for k in KINDS}
DEFAULT_KIND = "bug"

MAX_MESSAGE = 4000
MAX_EMAIL = 200
MIN_MESSAGE = 10          # "it's broken" is not a report; ask for a sentence

# One submission per reporter per interval. A public form that sends email is a
# spam relay otherwise.
THROTTLE_SECONDS = 30


def kind_label(slug: str) -> str:
    k = KIND_BY_SLUG.get(slug)
    return k["label"] if k else slug


def destination() -> str:
    """Where reports are emailed.

    `FEEDBACK_TO` if set, otherwise the owner account that already gates
    `/admin` — the hosted deployment has one address configured either way, and
    a self-hosted instance doesn't have to learn a second env var. Empty means
    nowhere: the report is still recorded, and `submit` says so.
    """
    return (os.environ.get("FEEDBACK_TO", "").strip()
            or auth.owner_email())


def support_address() -> str:
    """A human-readable address to show as an alternative to the form.

    Empty unless `SUPPORT_EMAIL` is set, so an unconfigured deployment renders
    no address rather than a dead `mailto:` — the same fail-closed shape as
    `auth.owner_email()`.
    """
    return os.environ.get("SUPPORT_EMAIL", "").strip()


def clean(kind: str, message: str, reply_to: str) -> tuple[str, str, str]:
    """Normalise the three submitted fields. Raises ValueError with a message
    the form can show verbatim."""
    kind = kind if kind in KIND_BY_SLUG else DEFAULT_KIND
    message = (message or "").strip()[:MAX_MESSAGE]
    reply_to = (reply_to or "").strip()[:MAX_EMAIL]
    if len(message) < MIN_MESSAGE:
        raise ValueError(
            "Please describe the problem in a sentence or two — enough that it "
            "can be reproduced."
        )
    if reply_to and "@" not in reply_to:
        raise ValueError("That doesn't look like an email address.")
    return kind, message, reply_to


def _body(kind: str, message: str, who: str, account: str | None) -> str:
    """The email, with every piece of user text escaped.

    The message is the reporter's words; it arrives as text in an HTML email, so
    it is escaped and wrapped rather than interpolated.
    """
    lines = [
        f"<p><strong>{html.escape(kind_label(kind))}</strong></p>",
        f"<pre style='white-space:pre-wrap;font:inherit'>{html.escape(message)}</pre>",
        "<hr>",
        f"<p style='color:#667'>From: {html.escape(who or 'anonymous visitor')}",
    ]
    if account:
        lines.append(f"<br>Account: {html.escape(account)}")
    lines.append("</p>")
    return "\n".join(lines)


def submit(kind: str, message: str, reply_to: str,
           user_id: int | None = None, account_email: str | None = None) -> bool:
    """Record a report and try to email it. True if it was also sent.

    The row is written first and unconditionally: delivery is the nice-to-have,
    durability is the requirement.
    """
    storage.add_feedback(kind, message, reply_to or None, user_id)

    to = destination()
    if not to:
        return False

    who = reply_to or account_email or ""
    subject = f"[Networthy HQ] {kind_label(kind)}" + (f" — {who}" if who else "")
    return mailer.send_email(
        to, subject, _body(kind, message, who, account_email),
        # Replying to the notification reaches the reporter directly, which is
        # the whole difference between a feedback box and a suggestion box.
        reply_to=reply_to or account_email or None,
    )
