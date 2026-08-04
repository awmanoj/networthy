"""Email digests: a short daily net-worth pulse and a longer weekly breakdown.

Run from cron (inside the container), e.g.:
    python -m app.digest daily     # 6 PM IST  -> day-over-day change
    python -m app.digest weekly    # weekly    -> + MF/Equity/Foreign/Crypto moves

Each run recomputes every user's net worth live (same path as the dashboard),
records a daily snapshot in `nw_history`, and emails a change summary. Only the
live-priced categories (mutual funds, equity, foreign equity, crypto) move day to
day, so those are what the weekly breakdown covers. One bad user never sinks the
batch. Emails go out via `mailer` (which no-ops to a log without RESEND_API_KEY).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone

from . import mailer, storage

log = logging.getLogger("networthy.digest")

IST = timezone(timedelta(hours=5, minutes=30))

# The live-priced categories that actually move — (leaf slug, display label).
LIVE_CATEGORIES = [
    ("mutual-funds", "Mutual Funds"),
    ("equity", "Equity"),
    ("foreign-equity", "Foreign / US Equity"),
    ("crypto", "Crypto"),
]

# Ink Navy & Copper — inline hexes (email clients can't use CSS variables).
_INK = "#16202b"; _MUTED = "#566373"; _FAINT = "#8792a0"; _NAVY = "#1f3a5f"
_GAIN = "#1f8a6b"; _LOSS = "#cf5a4e"; _PAPER = "#eceef1"; _SURFACE = "#ffffff"
_BORDER = "#d8dde4"


def ist_today() -> date:
    return datetime.now(IST).date()


def _inr(n: float) -> str:
    return f"₹{n:,.0f}"


def _pct(delta: float, base: float) -> float | None:
    return (delta / base * 100.0) if base else None


def _change_words(delta: float, base: float, period: str) -> str:
    if abs(delta) < 1:
        return f"No change {period}."
    arrow = "▲" if delta > 0 else "▼"
    pct = _pct(delta, base)
    pct_s = f" ({pct:+.1f}%)" if pct is not None else ""
    return f"{arrow} {_inr(abs(delta))}{pct_s} {period}"


# --- Net-worth computation (reuses the dashboard logic) ---------------------

def _compute(user) -> tuple[dict, dict]:
    """(dashboard dict, {live-slug: value}) for a user, priced live."""
    from .main import _dashboard, _leaf_value  # local import avoids import-time cost
    dash = _dashboard(user)
    live = {slug: (_leaf_value(user, slug) or 0.0) for slug, _ in LIVE_CATEGORIES}
    return dash, live


def _record(user_id: int, day: date, dash: dict, live: dict) -> None:
    storage.record_nw_snapshot(
        user_id, day.isoformat(), dash["net_worth"], dash["assets"],
        dash["liabilities"], json.dumps(live),
    )


# --- Email shell ------------------------------------------------------------

def _shell(body: str) -> str:
    """Wrap body rows in the branded, client-robust table layout."""
    return f"""\
<!doctype html><html><body style="margin:0;padding:0;background:{_PAPER};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:{_PAPER};padding:32px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:480px;background:{_SURFACE};border:1px solid {_BORDER};
                    border-radius:14px;overflow:hidden;
                    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
        <tr><td style="padding:22px 28px 6px;">
          <span style="font-size:17px;font-weight:700;letter-spacing:-0.01em;color:{_INK};">₹ Networthy HQ</span>
        </td></tr>
        {body}
        <tr><td style="padding:16px 28px;background:{_PAPER};border-top:1px solid {_BORDER};">
          <p style="margin:0;font-size:12px;line-height:1.5;color:{_FAINT};">
            Valued live where we can · your data stays private to your account. networthyhq.com
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _hero_row(label: str, net: float, change_html: str) -> str:
    return f"""\
        <tr><td style="padding:6px 28px 0;">
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.12em;color:{_FAINT};font-weight:700;">{label}</div>
          <div style="font-size:36px;font-weight:700;letter-spacing:-0.02em;color:{_INK};margin:6px 0 4px;">{_inr(net)}</div>
          {change_html}
        </td></tr>"""


# --- Daily digest -----------------------------------------------------------

def daily_email(day: date, dash: dict, prev: dict | None) -> tuple[str, str]:
    net = dash["net_worth"]
    if prev is None:
        change = (f'<div style="font-size:14px;color:{_MUTED};">'
                  f"We've started tracking — you'll see your daily change from tomorrow.</div>")
        subject = "Your net-worth pulse · Networthy HQ"
    else:
        delta = net - prev["net_worth"]
        color = _GAIN if delta > 0 else _LOSS if delta < 0 else _MUTED
        change = (f'<div style="font-size:15px;font-weight:600;color:{color};">'
                  f"{_change_words(delta, prev['net_worth'], 'since yesterday')}</div>")
        # Subject carries the CHANGE, not the total — the absolute figure stays out
        # of inbox previews / lock screens.
        if abs(delta) < 1:
            subject = "Flat today · Networthy HQ"
        else:
            subject = f"{'▲' if delta > 0 else '▼'} {_inr(abs(delta))} today · Networthy HQ"
    body = _hero_row(f"Net worth · {day.strftime('%d %b %Y')}", net, change)
    return _shell(body), subject


# --- Weekly digest ----------------------------------------------------------

def _breakdown_rows(live_now: dict, base: dict | None) -> str:
    rows = ""
    for slug, label in LIVE_CATEGORIES:
        now = live_now.get(slug, 0.0)
        if not now and not (base and base.get(slug)):
            continue  # skip categories the user doesn't hold
        if base is not None and base.get(slug):
            d = now - base[slug]
            c = _GAIN if d > 0 else _LOSS if d < 0 else _FAINT
            pct = _pct(d, base[slug])
            chg = f'{("▲" if d>0 else "▼" if d<0 else "·")} {_inr(abs(d))}' + (f" ({pct:+.1f}%)" if pct is not None else "")
        else:
            c, chg = _FAINT, "—"
        rows += f"""\
          <tr>
            <td style="padding:9px 0;border-top:1px solid {_BORDER};font-size:14px;color:{_INK};">{label}</td>
            <td style="padding:9px 0;border-top:1px solid {_BORDER};font-size:14px;color:{_INK};text-align:right;font-variant-numeric:tabular-nums;">{_inr(now)}</td>
            <td style="padding:9px 0;border-top:1px solid {_BORDER};font-size:13px;color:{c};text-align:right;font-variant-numeric:tabular-nums;">{chg}</td>
          </tr>"""
    return rows


def weekly_email(day: date, dash: dict, live_now: dict, base: dict | None) -> tuple[str, str]:
    net = dash["net_worth"]
    base_net = base.get("net_worth") if base else None
    base_live = json.loads(base["breakdown"]) if (base and base.get("breakdown")) else None

    if base_net:
        delta = net - base_net
        color = _GAIN if delta > 0 else _LOSS if delta < 0 else _MUTED
        change = (f'<div style="font-size:15px;font-weight:600;color:{color};">'
                  f"{_change_words(delta, base_net, 'this week')}</div>")
        subject = ("Flat this week · Networthy HQ" if abs(delta) < 1
                   else f"{'▲' if delta > 0 else '▼'} {_inr(abs(delta))} this week · Networthy HQ")
    else:
        change = (f'<div style="font-size:14px;color:{_MUTED};">'
                  f"Your first weekly summary — week-over-week changes start next week.</div>")
        subject = "Your weekly net-worth summary · Networthy HQ"

    rows = _breakdown_rows(live_now, base_live)
    table = ""
    if rows:
        table = f"""\
        <tr><td style="padding:20px 28px 4px;">
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.12em;color:{_FAINT};font-weight:700;">What moved</div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:6px;">
            {rows}
          </table>
        </td></tr>"""

    body = _hero_row(f"Net worth · {day.strftime('%d %b %Y')}", net, change) + table
    return _shell(body), subject


# --- Runners ----------------------------------------------------------------

def _valid_email(e: str | None) -> bool:
    return bool(e) and "@" in e


def run_daily() -> int:
    storage.init_db()
    day = ist_today()
    sent = 0
    for user in storage.list_users():
        try:
            dash, live = _compute(user)
            if not dash["has_data"]:
                continue
            prev = storage.latest_nw_snapshot_before(user.id, day.isoformat())
            _record(user.id, day, dash, live)
            if _valid_email(user.email):
                html, subject = daily_email(day, dash, prev)
                mailer.send_email(user.email, subject, html)
                sent += 1
        except Exception:  # noqa: BLE001 — one user must not sink the batch
            log.exception("daily digest failed for user %s", user.id)
    return sent


def run_weekly() -> int:
    storage.init_db()
    day = ist_today()
    week_ago = (day - timedelta(days=7)).isoformat()
    sent = 0
    for user in storage.list_users():
        try:
            dash, live = _compute(user)
            if not dash["has_data"]:
                continue
            base = storage.nw_snapshot_on_or_before(user.id, week_ago)
            _record(user.id, day, dash, live)  # also keep today's point
            if _valid_email(user.email):
                html, subject = weekly_email(day, dash, live, base)
                mailer.send_email(user.email, subject, html)
                sent += 1
        except Exception:  # noqa: BLE001
            log.exception("weekly digest failed for user %s", user.id)
    return sent


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "daily":
        n = run_daily()
    elif mode == "weekly":
        n = run_weekly()
    else:
        print("usage: python -m app.digest {daily|weekly}", file=sys.stderr)
        return 2
    log.info("%s digest: %d email(s) sent", mode, n)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
