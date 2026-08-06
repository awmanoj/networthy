"""Business analytics — adoption metrics for the owner, computed live from the app's
own tables. First-party by design: nothing here egresses, and it deliberately reads
only account/usage **metadata** (signups, logins, which features were touched), never
any financial values — so this surface holds no holdings data even if it leaked.

The shared demo account is excluded from every number.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import storage
from .demo import DEMO_EMAIL

# (label, table, extra WHERE condition or None) — a "feature" is used once a user has
# at least one row here. Counts distinct users, demo excluded.
_FEATURES: list[tuple[str, str, str | None]] = [
    ("Uploaded NSDL CAS", "snapshots", None),
    ("Imported CAMS", "networth_holdings", "source = 'cams'"),
    ("Set a goal", "goals", None),
    ("Tracked expenses", "expenses", None),
    ("Real estate", "property_holdings", None),
    ("Crypto", "crypto_holdings", None),
    ("US equity", "foreign_holdings", None),
    ("Fixed income (manual)", "manual_holdings", None),
    ("Bank & cash", "bank_cash", None),
    ("Physical gold", "gold_items", None),
    ("Liabilities", "liabilities", None),
    ("Alternate investments", "alt_investments", None),
]

# Tables that mean "this user put real data in" — for the activation funnel.
_ASSET_TABLES = [
    "snapshots", "networth_holdings", "property_holdings", "bank_cash",
    "manual_holdings", "gold_items", "alt_investments", "crypto_holdings",
    "foreign_holdings", "forex_holdings", "business_holdings", "liabilities",
    "goals", "expenses",
]


def _iso(days_ago: int) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def overview() -> dict:
    with storage._connect() as conn:
        demo = conn.execute("SELECT id FROM users WHERE email = ?", (DEMO_EMAIL,)).fetchone()
        demo_id = demo["id"] if demo else -1

        def scalar(sql: str, params: tuple = ()) -> int:
            return conn.execute(sql, params).fetchone()[0]

        total = scalar("SELECT COUNT(*) FROM users WHERE id != ?", (demo_id,))
        new_today = scalar(
            "SELECT COUNT(*) FROM users WHERE id != ? AND created_at >= ?",
            (demo_id, _iso(1)))
        new_7d = scalar(
            "SELECT COUNT(*) FROM users WHERE id != ? AND created_at >= ?",
            (demo_id, _iso(7)))
        new_30d = scalar(
            "SELECT COUNT(*) FROM users WHERE id != ? AND created_at >= ?",
            (demo_id, _iso(30)))

        # Sign-ins (from the durable login_events log).
        logins_total = scalar("SELECT COUNT(*) FROM login_events WHERE user_id != ?", (demo_id,))
        logins_24h = scalar(
            "SELECT COUNT(DISTINCT user_id) FROM login_events WHERE user_id != ? AND created_at >= ?",
            (demo_id, _iso(1)))
        logins_7d = scalar(
            "SELECT COUNT(DISTINCT user_id) FROM login_events WHERE user_id != ? AND created_at >= ?",
            (demo_id, _iso(7)))
        logins_30d = scalar(
            "SELECT COUNT(DISTINCT user_id) FROM login_events WHERE user_id != ? AND created_at >= ?",
            (demo_id, _iso(30)))
        # Returning = signed in on ≥2 distinct days.
        returning = scalar(
            """
            SELECT COUNT(*) FROM (
                SELECT user_id FROM login_events WHERE user_id != ?
                GROUP BY user_id HAVING COUNT(DISTINCT date(created_at)) >= 2
            )
            """, (demo_id,))

        # Cumulative signups over time (for the growth chart).
        rows = conn.execute(
            "SELECT date(created_at) d, COUNT(*) n FROM users WHERE id != ? GROUP BY d ORDER BY d",
            (demo_id,)).fetchall()
        cum, chart = 0, []
        for r in rows:
            cum += r["n"]
            chart.append({"date": r["d"], "value": cum})

        # Feature adoption.
        features = []
        for label, table, cond in _FEATURES:
            where = f"user_id != ?" + (f" AND {cond}" if cond else "")
            n = scalar(f"SELECT COUNT(DISTINCT user_id) FROM {table} WHERE {where}", (demo_id,))
            features.append({"label": label, "users": n,
                             "pct": (n / total * 100.0) if total else 0.0})
        features.sort(key=lambda f: f["users"], reverse=True)

        # Activation funnel: signed up → put in real data → came back.
        union = " UNION ".join(
            f"SELECT user_id FROM {t} WHERE user_id != {demo_id}" for t in _ASSET_TABLES)
        activated = scalar(f"SELECT COUNT(*) FROM (SELECT DISTINCT user_id FROM ({union}))")
        funnel = [
            {"label": "Signed up", "users": total, "pct": 100.0},
            {"label": "Added real data", "users": activated,
             "pct": (activated / total * 100.0) if total else 0.0},
            {"label": "Came back (≥2 days)", "users": returning,
             "pct": (returning / total * 100.0) if total else 0.0},
        ]

        # Recent signups with a last-seen — the email listing.
        recent = conn.execute(
            """
            SELECT u.email, u.created_at,
                   (SELECT MAX(created_at) FROM login_events e WHERE e.user_id = u.id) last_seen
            FROM users u WHERE u.id != ?
            ORDER BY u.created_at DESC LIMIT 200
            """, (demo_id,)).fetchall()

    def fmt(s: str | None) -> str | None:
        if not s:
            return None
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y")
        except ValueError:
            return s

    return {
        "total_users": total, "new_today": new_today, "new_7d": new_7d, "new_30d": new_30d,
        "returning": returning, "logins_total": logins_total,
        "logins_24h": logins_24h, "logins_7d": logins_7d, "logins_30d": logins_30d,
        "signup_chart": chart, "features": features, "funnel": funnel,
        "recent": [{"email": r["email"], "joined": fmt(r["created_at"]),
                    "last_seen": fmt(r["last_seen"])} for r in recent],
    }
