"""Take your data out — the other half of "it's your data".

A tracker that can't export is a roach motel, and the privacy page's promise is
only worth something if leaving is a button rather than an email. This module is
the whole of that: it reads every table that belongs to a user and writes it as
JSON (complete, structured, re-importable in principle) or as a zip of CSVs (one
per table, openable in any spreadsheet).

Two decisions worth knowing:

* **The table list is explicit, not derived.** It would be tidier to walk
  `sqlite_master` for anything with a `user_id`, but then a future table joins
  the export silently — including one holding something that shouldn't leave in a
  plain file. Adding a table here is a deliberate act.
* **`sessions` and `login_codes` are excluded on purpose.** They're live
  credentials, not data about you; a downloaded file containing a valid session
  token is a security hole, not a feature.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime

from . import storage

# Every table whose rows belong to the user, in a sensible reading order.
# `holdings` is special: it's keyed by snapshot_id rather than user_id, so it's
# scoped through its parent snapshot (see `_rows_for`).
EXPORT_TABLES: list[str] = [
    # The Networth tree
    "property_holdings", "bank_cash", "manual_holdings", "gold_items",
    "alt_investments", "crypto_holdings", "foreign_holdings", "forex_holdings",
    "business_holdings", "liabilities", "networth_holdings",
    # Planning
    "expenses", "goals",
    # Statements and history
    "snapshots", "holdings", "nw_history",
    # Preferences (PAN, withdrawal rate, plan inputs) — easy to forget, and the
    # part a user would most notice missing after moving machines.
    "user_settings",
]

# Held about the user but deliberately not exported: live session tokens and
# unconsumed login codes are credentials, and `login_events` is operational
# metadata rather than anything they entered.
_EXCLUDED = ("sessions", "login_codes", "login_events", "users", "app_state")


def _rows_for(conn, table: str, user_id: int) -> list[dict]:
    if table == "holdings":
        # Scoped via the parent snapshot, and carries the statement date so the
        # rows mean something on their own once they're out of the database.
        sql = (
            "SELECT h.*, s.statement_date FROM holdings h "
            "JOIN snapshots s ON s.id = h.snapshot_id "
            "WHERE s.user_id = ? ORDER BY s.statement_date, h.position"
        )
    else:
        sql = f"SELECT * FROM {table} WHERE user_id = ?"
    return [dict(r) for r in conn.execute(sql, (user_id,))]


def collect(user_id: int) -> dict[str, list[dict]]:
    """Every exportable row for a user, keyed by table name."""
    with storage._connect() as conn:
        return {t: _rows_for(conn, t, user_id) for t in EXPORT_TABLES}


def as_json(user_id: int, email: str) -> str:
    """The complete export as JSON, with a small header for provenance."""
    data = collect(user_id)
    payload = {
        "networthy_export": {
            "version": 1,
            "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "account": email,
            "row_counts": {t: len(rows) for t, rows in data.items()},
            "note": (
                "Every figure you entered or imported, straight from the database. "
                "Session tokens and login codes are deliberately excluded."
            ),
        },
        "data": data,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default)


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def as_csv_zip(user_id: int, email: str) -> bytes:
    """One CSV per non-empty table, zipped.

    Empty tables are skipped: a zip of thirty files, twenty-five of them a lone
    header row, is worse than a zip of five that hold something.
    """
    data = collect(user_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for table, rows in data.items():
            if not rows:
                continue
            out = io.StringIO()
            writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(f"networthy/{table}.csv", out.getvalue())
        zf.writestr("networthy/README.txt", _readme(email, data))
    return buf.getvalue()


def _readme(email: str, data: dict[str, list[dict]]) -> str:
    lines = [
        "Networthy HQ — your data",
        "=" * 24,
        "",
        f"Account : {email}",
        f"Exported: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
        "One CSV per table. Amounts are in INR. Empty tables are omitted.",
        "",
    ]
    for table, rows in data.items():
        if rows:
            lines.append(f"  {table}.csv — {len(rows)} row{'' if len(rows) == 1 else 's'}")
    lines += [
        "",
        "Not included: session tokens and login codes. Those are credentials,",
        "not your data, and shouldn't sit in a file in your downloads folder.",
        "",
        "You can run Networthy on your own machine: uvx networthy",
    ]
    return "\n".join(lines) + "\n"


def delete_everything(user_id: int) -> dict[str, int]:
    """Erase the account's data. Returns rows removed per table.

    Covers exactly what `collect` returns, so "download everything" and "delete
    everything" can't drift apart — the failure mode being a table that quietly
    survives deletion after the user was told it was gone.

    The `users` row itself stays: it's the identity the session and any future
    login hangs off, and removing it while a request is in flight is a sharper
    edge than it's worth. Nothing personal remains beyond the email address.
    """
    removed: dict[str, int] = {}
    with storage._connect() as conn:
        # Snapshots last: deleting them cascades to `holdings`, which has no
        # user_id of its own.
        ordered = [t for t in EXPORT_TABLES if t not in ("holdings", "snapshots")]
        ordered += ["snapshots"]
        for table in ordered:
            cur = conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            if cur.rowcount:
                removed[table] = cur.rowcount
    return removed
