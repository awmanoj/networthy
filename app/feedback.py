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

import hashlib
import hmac
import html
import os
import re
import time

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

# --- Spam defence -----------------------------------------------------------
#
# Deliberately no CAPTCHA. Every hosted CAPTCHA is a third-party script that
# would watch a page this site promises is unwatched, and the self-hosted ones
# tax the exact person we most want to hear from — someone annoyed enough by a
# bug to fill in a form. All three layers below are invisible to a human.
#
# The failure mode is chosen too: suspected spam is **stored and not emailed**,
# never rejected. A false positive costs a report sitting in /admin instead of
# an inbox; rejecting would lose a real bug report with no trace of it.

# Nothing human reads a form and writes a usable bug report in three seconds.
MIN_FILL_SECONDS = 3
# Past this the timing signal is meaningless — a tab left open over a weekend is
# not evidence of anything, so it stops counting rather than becoming suspicious.
TOKEN_MAX_AGE = 48 * 3600

_URL_RE = re.compile(r"(https?://|www\.)\S*", re.I)
# Our own links don't count. The form asks reporters to say which page they were
# on, so penalising them for pasting it would punish following the instructions.
_OWN_LINK_RE = re.compile(r"(https?://)?(www\.)?networthyhq\.com\S*", re.I)
# Bots echo the page they scraped. The sample that prompted this ended
# "— report a bug · networthy hq", which is this site's own <title>.
_ECHO_RE = re.compile(r"report a bug|networthy\s*hq", re.I)
# Generic contact-bait with no reference to anything on the page.
_TEMPLATE_RE = re.compile(
    r"(more info(rmation)?\b.{0,40}(contact|e-?mail)"
    r"|contact me (by|via|on|at) (e-?mail|whatsapp|telegram))",
    re.I,
)
# Vocabulary that is categorically not bug-report vocabulary. Nobody describing
# a broken page reaches for "backlinks". Strong enough to act on alone, unlike
# the softer signals above — which is why "partnership" and "collaborate" are
# deliberately absent: someone offering to contribute to the project would use
# both, and that's a message worth receiving.
_SOLICIT_RE = re.compile(
    r"(seo services|back-?links?|link.?building|guest post"
    r"|digital marketing|increase your (traffic|ranking|sales)"
    r"|web ?(site )?design services|crypto ?investment opportunit)",
    re.I,
)
# Two independent soft signals before we act. One alone is ordinary: a real
# reporter may well paste a link, or name the page they were on.
_SPAM_SCORE_THRESHOLD = 2


def _secret() -> bytes:
    return (os.environ.get("APP_SECRET") or "networthy-dev").encode()


def issue_token(now: float | None = None) -> str:
    """A signed timestamp, rendered into the form when it's served.

    Signed rather than plain so it can't be back-dated, and stateless so it
    costs no storage and works across processes.
    """
    ts = int(now if now is not None else time.time())
    sig = hmac.new(_secret(), str(ts).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def token_age(token: str, now: float | None = None) -> float | None:
    """Seconds since the form was served, or None if the token isn't ours."""
    try:
        raw, sig = (token or "").split(".", 1)
        ts = int(raw)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_secret(), str(ts).encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return (now if now is not None else time.time()) - ts


def spam_reason(message: str, token: str, honeypot: str,
                now: float | None = None) -> str | None:
    """Why this looks automated, or None if it looks like a person.

    Returned as a string rather than a bool so /admin can show *why* something
    was held back — a filter whose decisions can't be inspected is one nobody
    trusts enough to leave switched on.
    """
    if honeypot.strip():
        return "filled a field that is invisible to humans"

    age = token_age(token, now)
    if age is None:
        return "posted without a valid form token"
    if age < MIN_FILL_SECONDS:
        return f"submitted {age:.1f}s after the form loaded"

    # Links are examined first and then removed, so the words inside a URL can't
    # also trip the title-echo rule — "networthyhq.com" is not someone quoting
    # the page title back at us.
    external = _URL_RE.sub(lambda m: "" if _OWN_LINK_RE.fullmatch(m.group()) else m.group(),
                           message)
    prose = _URL_RE.sub(" ", message)

    if _SOLICIT_RE.search(prose):
        return "sales solicitation, not a bug report"

    signals = []
    if _URL_RE.search(external):
        signals.append("contains a link to somewhere else")
    if _ECHO_RE.search(prose):
        signals.append("echoes the page title back")
    if _TEMPLATE_RE.search(prose):
        signals.append("generic contact template")
    if len(signals) >= _SPAM_SCORE_THRESHOLD:
        return ", ".join(signals)
    return None


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
           user_id: int | None = None, account_email: str | None = None,
           spam: str | None = None) -> bool:
    """Record a report and try to email it. True if it was also sent.

    The row is written first and unconditionally: delivery is the nice-to-have,
    durability is the requirement. Suspected spam is stored and *not* emailed —
    kept because the filter can be wrong, unsent because the whole point is an
    inbox worth reading.
    """
    storage.add_feedback(kind, message, reply_to or None, user_id, spam=spam)
    if spam:
        return False

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
