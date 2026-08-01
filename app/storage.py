"""SQLite persistence for accounts, sessions, and net-worth snapshots.

Uses the stdlib sqlite3 driver to keep dependencies minimal. The database lives
under data/ which is gitignored — parsed financial data never leaves the machine.

Multi-user: every snapshot belongs to a user, and all snapshot queries are scoped
by user_id so accounts can't see or mutate each other's data.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import Account, Holding, Snapshot, User

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "networthy.db"

# Store timestamps in the same format sqlite's datetime('now') emits (UTC), so
# string comparisons like `expires_at > datetime('now')` are correct.
_DB_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --- Schema & migration -----------------------------------------------------

def init_db() -> None:
    """Create tables if missing and migrate legacy single-tenant data.

    Safe (idempotent) to call on every startup.
    """
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_codes (
                email      TEXT PRIMARY KEY,
                code_hash  TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                statement_date  TEXT NOT NULL,
                total_value     REAL NOT NULL,
                holding_count   INTEGER NOT NULL DEFAULT 0,
                source_filename TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_id, statement_date)
            )
            """
        )
        # Created after the legacy migration below, which renames/recreates the
        # snapshots table this FK points at — building holdings first would let
        # SQLite rewrite the reference onto the dropped legacy table.
        _migrate_legacy_snapshots(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS holdings (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id        INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                account_kind       TEXT,
                account_name       TEXT,
                account_identifier TEXT,
                depository         TEXT,
                position           INTEGER NOT NULL DEFAULT 0,
                isin               TEXT,
                name               TEXT NOT NULL,
                asset_class        TEXT,
                units              REAL,
                price              REAL,
                value              REAL,
                ticker             TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_holdings_snapshot ON holdings(snapshot_id)"
        )
        _add_column_if_missing(conn, "holdings", "ticker", "TEXT")
        # Imported holdings that populate the Networth breakdown pages (e.g. a CAMS
        # mutual-fund CAS). Kept separate from `snapshots`/`holdings` on purpose: a
        # snapshot is *total* net worth and drives the dashboard chart, whereas a
        # CAMS import is mutual-fund-only and must never land on that timeline.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS networth_holdings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source      TEXT NOT NULL,        -- 'cams'
                as_of_date  TEXT,
                asset_class TEXT,
                name        TEXT NOT NULL,
                isin        TEXT,
                folio       TEXT,
                units       REAL,
                price       REAL,
                value       REAL,
                position    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_networth_user "
            "ON networth_holdings(user_id, asset_class)"
        )
        # Hand-entered holdings for Networth leaves that no statement covers (PPF,
        # EPF, NSC, SSA, Others) or that supplement a CAS (bonds held off-demat).
        # investment_amount is the current value that rolls into net worth; the
        # rest are optional context/projection.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_holdings (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                leaf_slug         TEXT NOT NULL,
                scheme            TEXT NOT NULL,
                investment_amount REAL NOT NULL,
                maturity_amount   REAL,
                investment_date   TEXT,
                maturity_date     TEXT,
                years             REAL,
                rate              REAL,
                position          INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_user_leaf "
            "ON manual_holdings(user_id, leaf_slug)"
        )
        # Hand-entered foreign (US) equity: ticker + shares, priced live in USD via
        # Yahoo and converted to INR. Separate from manual_holdings because it's
        # ticker/shares-shaped, not a fixed rupee amount.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS foreign_holdings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ticker     TEXT NOT NULL,
                units      REAL NOT NULL,
                cost_usd   REAL,
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_foreign_user ON foreign_holdings(user_id)"
        )
        # Hand-entered foreign-currency money (Foreign Exchange leaf): an amount in a
        # currency, held in an account or as cash, valued live in INR at the FX rate.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forex_holdings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                currency   TEXT NOT NULL,
                amount     REAL NOT NULL,
                kind       TEXT,
                label      TEXT,
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forex_user ON forex_holdings(user_id)"
        )
        # Hand-entered alternate investments (Alternate Investments leaf): illiquid,
        # hand-valued bets — startups/angel, ESOPs, unlisted equity, PE/VC, crypto.
        # current_value is the mark that rolls into net worth; cost enables gain%.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alt_investments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                category      TEXT,
                cost          REAL,
                current_value REAL NOT NULL,
                invested_date TEXT,
                position      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alt_user ON alt_investments(user_id)"
        )
        # Hand-entered real estate, one table across the five Real Estate sub-leaves
        # (keyed by leaf_slug). current_value (gross market value) rolls into net worth
        # — any loan against it is tracked separately under Liabilities. cost enables
        # gain%; notes hold location/size.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS property_holdings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                leaf_slug     TEXT NOT NULL,
                label         TEXT NOT NULL,
                current_value REAL NOT NULL,
                cost          REAL,
                purchase_date TEXT,
                notes         TEXT,
                position      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_property_user_leaf "
            "ON property_holdings(user_id, leaf_slug)"
        )
        # Hand-entered bank accounts and cash. One table for both leaves, keyed by
        # leaf_slug ('bank-accounts' | 'cash'); balance is the value that rolls into
        # net worth. Bank rows carry bank_name/account_type; cash rows leave them null.
        # (No account number is stored, by design.)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_cash (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                leaf_slug    TEXT NOT NULL,
                bank_name    TEXT,
                account_type TEXT,
                label        TEXT,
                balance      REAL NOT NULL,
                position     INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bank_cash_user_leaf "
            "ON bank_cash(user_id, leaf_slug)"
        )
        # Dates were added after the initial manual_holdings shape; `years` is kept
        # only as a display fallback for any rows entered before dates existed.
        _add_column_if_missing(conn, "manual_holdings", "investment_date", "TEXT")
        _add_column_if_missing(conn, "manual_holdings", "maturity_date", "TEXT")


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add `column` to `table` if an older DB predates it. Idempotent."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate_legacy_snapshots(conn: sqlite3.Connection) -> None:
    """Move pre-multi-user snapshots (no user_id) onto the OWNER_EMAIL account.

    The original schema had a single global snapshots table keyed by
    UNIQUE(statement_date). If we detect that shape (a snapshots table without a
    user_id column), rename it aside, recreate the multi-user table, and copy the
    rows across under the owner's user id.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(snapshots)")}
    if "user_id" in cols:
        return  # already migrated (or created fresh with the new schema)

    owner_email = _normalize_email(os.environ.get("OWNER_EMAIL", "owner@localhost"))
    owner_id = _get_or_create_user(conn, owner_email)

    conn.execute("ALTER TABLE snapshots RENAME TO snapshots_legacy")
    conn.execute(
        """
        CREATE TABLE snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            statement_date  TEXT NOT NULL,
            total_value     REAL NOT NULL,
            holding_count   INTEGER NOT NULL DEFAULT 0,
            source_filename TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (user_id, statement_date)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO snapshots
            (user_id, statement_date, total_value, holding_count, source_filename, created_at)
        SELECT ?, statement_date, total_value, holding_count, source_filename, created_at
        FROM snapshots_legacy
        """,
        (owner_id,),
    )
    conn.execute("DROP TABLE snapshots_legacy")


# --- Users ------------------------------------------------------------------

def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _get_or_create_user(conn: sqlite3.Connection, email: str) -> int:
    """Return the user id for email, creating the account if needed."""
    email = _normalize_email(email)
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute("INSERT INTO users (email) VALUES (?)", (email,))
    return int(cur.lastrowid)


def get_or_create_user(email: str) -> User:
    with _connect() as conn:
        user_id = _get_or_create_user(conn, email)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row)


def get_user(user_id: int) -> User | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


# --- Login codes (OTP) ------------------------------------------------------

def create_login_code(email: str, code_hash: str, expires_at: datetime) -> None:
    """Store (replacing any existing) the active login code for an email."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO login_codes (email, code_hash, expires_at, attempts, created_at)
            VALUES (?, ?, ?, 0, datetime('now'))
            ON CONFLICT(email) DO UPDATE SET
                code_hash  = excluded.code_hash,
                expires_at = excluded.expires_at,
                attempts   = 0,
                created_at = datetime('now')
            """,
            (_normalize_email(email), code_hash, expires_at.strftime(_DB_TIME_FMT)),
        )


def get_active_login_code(email: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM login_codes WHERE email = ?",
            (_normalize_email(email),),
        ).fetchone()


def increment_code_attempts(email: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE login_codes SET attempts = attempts + 1 WHERE email = ?",
            (_normalize_email(email),),
        )


def consume_login_code(email: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM login_codes WHERE email = ?", (_normalize_email(email),)
        )


# --- Sessions ---------------------------------------------------------------

def create_session(user_id: int, token: str, expires_at: datetime) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at.strftime(_DB_TIME_FMT)),
        )


def get_session_user(token: str) -> User | None:
    """Return the user for a live (unexpired) session token, else None."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT u.* FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > datetime('now')
            """,
            (token,),
        ).fetchone()
    return _row_to_user(row) if row else None


def delete_session(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --- Snapshots (all user-scoped) --------------------------------------------

def upsert_snapshot(user_id: int, snapshot: Snapshot) -> int:
    """Insert a snapshot for a user, replacing any existing one for the same date.

    A given CAS date maps to exactly one net-worth figure per account, so
    re-uploading the same statement overwrites rather than duplicates. Returns the
    snapshot's row id (stable across an upsert) so holdings can be attached to it.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO snapshots
                (user_id, statement_date, total_value, holding_count, source_filename)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, statement_date) DO UPDATE SET
                total_value     = excluded.total_value,
                holding_count   = excluded.holding_count,
                source_filename = excluded.source_filename
            """,
            (
                user_id,
                snapshot.statement_date.isoformat(),
                snapshot.total_value,
                snapshot.holding_count,
                snapshot.source_filename,
            ),
        )
        row = conn.execute(
            "SELECT id FROM snapshots WHERE user_id = ? AND statement_date = ?",
            (user_id, snapshot.statement_date.isoformat()),
        ).fetchone()
    return int(row["id"])


def replace_holdings(snapshot_id: int, accounts: list[Account]) -> None:
    """Replace all detailed holdings stored for a snapshot with a fresh set.

    Re-parsing (or re-uploading) a statement rebuilds its holdings wholesale, so
    we clear then re-insert rather than diff. Order within each account is
    preserved via a `position` column so the UI renders rows as the CAS listed
    them.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM holdings WHERE snapshot_id = ?", (snapshot_id,))
        rows = []
        for account in accounts:
            for pos, h in enumerate(account.holdings):
                rows.append(
                    (
                        snapshot_id,
                        account.kind,
                        account.name,
                        account.identifier,
                        account.depository,
                        pos,
                        h.isin,
                        h.name,
                        h.asset_class,
                        h.units,
                        h.price,
                        h.value,
                        h.ticker,
                    )
                )
        if rows:
            conn.executemany(
                """
                INSERT INTO holdings
                    (snapshot_id, account_kind, account_name, account_identifier,
                     depository, position, isin, name, asset_class, units, price, value,
                     ticker)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


def list_accounts(snapshot_id: int) -> list[Account]:
    """Reconstruct the grouped Account/Holding tree stored for a snapshot."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM holdings WHERE snapshot_id = ?
            ORDER BY id ASC, position ASC
            """,
            (snapshot_id,),
        ).fetchall()

    accounts: list[Account] = []
    by_key: dict[tuple, Account] = {}
    for r in rows:
        key = (r["account_kind"], r["account_name"], r["account_identifier"])
        account = by_key.get(key)
        if account is None:
            account = Account(
                kind=r["account_kind"],
                name=r["account_name"],
                identifier=r["account_identifier"],
                depository=r["depository"],
            )
            by_key[key] = account
            accounts.append(account)
        account.holdings.append(
            Holding(
                name=r["name"],
                asset_class=r["asset_class"],
                isin=r["isin"],
                units=r["units"],
                price=r["price"],
                value=r["value"],
                ticker=r["ticker"],
            )
        )
    return accounts


def latest_snapshot(user_id: int) -> Snapshot | None:
    """The user's most recent snapshot by statement date, or None."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM snapshots WHERE user_id = ?
            ORDER BY statement_date DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def list_snapshots(user_id: int) -> list[Snapshot]:
    """Return a user's snapshots ordered oldest-first (chart-ready)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE user_id = ? ORDER BY statement_date ASC",
            (user_id,),
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def delete_snapshot(user_id: int, snapshot_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM snapshots WHERE id = ? AND user_id = ?",
            (snapshot_id, user_id),
        )


def delete_all_snapshots(user_id: int) -> None:
    """Remove all of a user's snapshots, returning their dashboard to empty."""
    with _connect() as conn:
        conn.execute("DELETE FROM snapshots WHERE user_id = ?", (user_id,))


# --- Networth breakdown holdings (imports, e.g. CAMS) -----------------------

def replace_networth_import(
    user_id: int,
    source: str,
    as_of_date: date | None,
    holdings: list[Holding],
) -> None:
    """Replace all imported holdings from a source with a fresh set.

    Re-uploading a CAMS statement rebuilds that source's rows wholesale (clear then
    re-insert), so the Networth pages always reflect the latest import.
    """
    as_of = as_of_date.isoformat() if as_of_date else None
    with _connect() as conn:
        conn.execute(
            "DELETE FROM networth_holdings WHERE user_id = ? AND source = ?",
            (user_id, source),
        )
        rows = [
            (
                user_id, source, as_of, h.asset_class, h.name, h.isin,
                None, h.units, h.price, h.value, pos,
            )
            for pos, h in enumerate(holdings)
        ]
        if rows:
            conn.executemany(
                """
                INSERT INTO networth_holdings
                    (user_id, source, as_of_date, asset_class, name, isin,
                     folio, units, price, value, position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


def list_networth_holdings(user_id: int, asset_classes: set[str]) -> list[dict]:
    """Imported holdings for the given asset classes, oldest-position first.

    Each row is a plain dict (name, isin, units, price, value, asset_class, source,
    as_of_date) — the Networth leaf pages render these directly.
    """
    if not asset_classes:
        return []
    classes = sorted(asset_classes)
    placeholders = ",".join("?" for _ in classes)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM networth_holdings
            WHERE user_id = ? AND asset_class IN ({placeholders})
            ORDER BY position ASC, id ASC
            """,
            (user_id, *classes),
        ).fetchall()
    return [_row_to_networth_holding(r) for r in rows]


def latest_holdings_by_class(user_id: int, asset_classes: set[str]) -> list[dict]:
    """Holdings of the given classes from the user's latest snapshot (NSDL CAS).

    This is the "reuse what we already parse" path — the same dict shape as
    ``list_networth_holdings``, tagged source='nsdl'.
    """
    if not asset_classes:
        return []
    snap = latest_snapshot(user_id)
    if snap is None:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM holdings WHERE snapshot_id = ? ORDER BY id ASC, position ASC",
            (snap.id,),
        ).fetchall()
    as_of = snap.statement_date.isoformat()
    return [
        {
            "name": r["name"],
            "isin": r["isin"],
            "units": r["units"],
            "price": r["price"],
            "value": r["value"],
            "asset_class": r["asset_class"],
            "ticker": r["ticker"],
            "source": "nsdl",
            "as_of_date": as_of,
        }
        for r in rows
        # Skip value-less rows (blank ISIN lines a prior parse stored) so they
        # don't clutter the Networth page for statements uploaded before the
        # parser started dropping them. They contribute nothing to totals anyway.
        if r["asset_class"] in asset_classes and r["value"] is not None
    ]


def delete_networth_import(user_id: int, source: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM networth_holdings WHERE user_id = ? AND source = ?",
            (user_id, source),
        )


# --- Manual holdings (hand-entered Networth leaf rows) -----------------------

def add_manual_holding(
    user_id: int,
    leaf_slug: str,
    scheme: str,
    investment_amount: float,
    maturity_amount: float | None = None,
    rate: float | None = None,
    investment_date: str | None = None,
    maturity_date: str | None = None,
) -> None:
    """Append one hand-entered holding to a Networth leaf.

    Dates are ISO strings (YYYY-MM-DD) or None. Tenure is derived from them at
    display time, so no separate years field is stored. New rows sort after
    existing ones (position = current count) so the list keeps entry order.
    """
    with _connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM manual_holdings WHERE user_id = ? AND leaf_slug = ?",
            (user_id, leaf_slug),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO manual_holdings
                (user_id, leaf_slug, scheme, investment_amount, maturity_amount,
                 investment_date, maturity_date, rate, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, leaf_slug, scheme, investment_amount, maturity_amount,
             investment_date, maturity_date, rate, count),
        )


def list_manual_holdings(user_id: int, leaf_slug: str) -> list[dict]:
    """A user's hand-entered holdings for one leaf, in entry order."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM manual_holdings
            WHERE user_id = ? AND leaf_slug = ?
            ORDER BY position ASC, id ASC
            """,
            (user_id, leaf_slug),
        ).fetchall()
    return [_row_to_manual(r) for r in rows]


def delete_manual_holding(user_id: int, holding_id: int) -> None:
    """Delete one manual holding, scoped to its owner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM manual_holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        )


# --- Foreign (US) equity holdings -------------------------------------------

def add_foreign_holding(
    user_id: int, ticker: str, units: float, cost_usd: float | None = None
) -> None:
    """Append one hand-entered foreign equity holding (ticker + shares)."""
    with _connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM foreign_holdings WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO foreign_holdings (user_id, ticker, units, cost_usd, position)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, ticker, units, cost_usd, count),
        )


def list_foreign_holdings(user_id: int) -> list[dict]:
    """A user's foreign equity holdings, in entry order."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM foreign_holdings WHERE user_id = ? ORDER BY position ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "ticker": r["ticker"],
            "units": r["units"],
            "cost_usd": r["cost_usd"],
        }
        for r in rows
    ]


def delete_foreign_holding(user_id: int, holding_id: int) -> None:
    """Delete one foreign equity holding, scoped to its owner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM foreign_holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        )


# --- Foreign currency (forex) holdings --------------------------------------

def add_forex_holding(
    user_id: int, currency: str, amount: float,
    kind: str | None = None, label: str | None = None,
) -> None:
    """Append one foreign-currency holding (amount in a currency, account or cash)."""
    with _connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM forex_holdings WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO forex_holdings (user_id, currency, amount, kind, label, position)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, currency, amount, kind, label, count),
        )


def list_forex_holdings(user_id: int) -> list[dict]:
    """A user's foreign-currency holdings, in entry order."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM forex_holdings WHERE user_id = ? ORDER BY position ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": r["id"], "currency": r["currency"], "amount": r["amount"],
            "kind": r["kind"], "label": r["label"],
        }
        for r in rows
    ]


def delete_forex_holding(user_id: int, holding_id: int) -> None:
    """Delete one forex holding, scoped to its owner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM forex_holdings WHERE id = ? AND user_id = ?",
            (holding_id, user_id),
        )


# --- Alternate investments --------------------------------------------------

def add_alt_investment(
    user_id: int, name: str, current_value: float,
    category: str | None = None, cost: float | None = None,
    invested_date: str | None = None,
) -> None:
    """Append one alternate investment (illiquid, hand-valued)."""
    with _connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM alt_investments WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO alt_investments
                (user_id, name, category, cost, current_value, invested_date, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, category, cost, current_value, invested_date, count),
        )


def list_alt_investments(user_id: int) -> list[dict]:
    """A user's alternate investments, in entry order."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM alt_investments WHERE user_id = ? ORDER BY position ASC, id ASC",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": r["id"], "name": r["name"], "category": r["category"],
            "cost": r["cost"], "current_value": r["current_value"],
            "invested_date": r["invested_date"],
        }
        for r in rows
    ]


def delete_alt_investment(user_id: int, inv_id: int) -> None:
    """Delete one alternate investment, scoped to its owner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM alt_investments WHERE id = ? AND user_id = ?",
            (inv_id, user_id),
        )


# --- Real estate (property) -------------------------------------------------

def add_property_holding(
    user_id: int, leaf_slug: str, label: str, current_value: float,
    cost: float | None = None, purchase_date: str | None = None,
    notes: str | None = None,
) -> None:
    """Append one property to a Real Estate sub-leaf."""
    with _connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM property_holdings WHERE user_id = ? AND leaf_slug = ?",
            (user_id, leaf_slug),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO property_holdings
                (user_id, leaf_slug, label, current_value, cost, purchase_date, notes, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, leaf_slug, label, current_value, cost, purchase_date, notes, count),
        )


def list_property_holdings(user_id: int, leaf_slug: str) -> list[dict]:
    """A user's properties for one Real Estate sub-leaf, in entry order.

    The date is exposed as ``invested_date`` so the shared gain/date enrichment
    (``main._enrich_alt``) applies unchanged.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM property_holdings
            WHERE user_id = ? AND leaf_slug = ?
            ORDER BY position ASC, id ASC
            """,
            (user_id, leaf_slug),
        ).fetchall()
    return [
        {
            "id": r["id"], "label": r["label"], "current_value": r["current_value"],
            "cost": r["cost"], "invested_date": r["purchase_date"], "notes": r["notes"],
        }
        for r in rows
    ]


def delete_property_holding(user_id: int, prop_id: int) -> None:
    """Delete one property, scoped to its owner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM property_holdings WHERE id = ? AND user_id = ?",
            (prop_id, user_id),
        )


# --- Bank accounts & cash ---------------------------------------------------

def add_bank_cash(
    user_id: int,
    leaf_slug: str,
    balance: float,
    bank_name: str | None = None,
    account_type: str | None = None,
    label: str | None = None,
) -> None:
    """Append one bank-account or cash entry to its leaf ('bank-accounts'|'cash')."""
    with _connect() as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM bank_cash WHERE user_id = ? AND leaf_slug = ?",
            (user_id, leaf_slug),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO bank_cash
                (user_id, leaf_slug, bank_name, account_type, label, balance, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, leaf_slug, bank_name, account_type, label, balance, count),
        )


def list_bank_cash(user_id: int, leaf_slug: str) -> list[dict]:
    """A user's bank/cash entries for one leaf, in entry order."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bank_cash
            WHERE user_id = ? AND leaf_slug = ?
            ORDER BY position ASC, id ASC
            """,
            (user_id, leaf_slug),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "bank_name": r["bank_name"],
            "account_type": r["account_type"],
            "label": r["label"],
            "balance": r["balance"],
        }
        for r in rows
    ]


def delete_bank_cash(user_id: int, entry_id: int) -> None:
    """Delete one bank/cash entry, scoped to its owner."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM bank_cash WHERE id = ? AND user_id = ?",
            (entry_id, user_id),
        )


# --- Row mappers ------------------------------------------------------------

def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        created_at=row["created_at"],
    )


def _row_to_networth_holding(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "isin": row["isin"],
        "units": row["units"],
        "price": row["price"],
        "value": row["value"],
        "asset_class": row["asset_class"],
        "source": row["source"],
        "as_of_date": row["as_of_date"],
    }


def _row_to_manual(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "leaf_slug": row["leaf_slug"],
        "scheme": row["scheme"],
        "investment_amount": row["investment_amount"],
        "maturity_amount": row["maturity_amount"],
        "investment_date": row["investment_date"],
        "maturity_date": row["maturity_date"],
        "years": row["years"],  # legacy fallback for rows entered before dates
        "rate": row["rate"],
    }


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        user_id=row["user_id"],
        statement_date=date.fromisoformat(row["statement_date"]),
        total_value=row["total_value"],
        holding_count=row["holding_count"],
        source_filename=row["source_filename"],
    )
