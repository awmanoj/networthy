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

from .models import Snapshot, User

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
        _migrate_legacy_snapshots(conn)


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

def upsert_snapshot(user_id: int, snapshot: Snapshot) -> None:
    """Insert a snapshot for a user, replacing any existing one for the same date.

    A given CAS date maps to exactly one net-worth figure per account, so
    re-uploading the same statement overwrites rather than duplicates.
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


# --- Row mappers ------------------------------------------------------------

def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        created_at=row["created_at"],
    )


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        user_id=row["user_id"],
        statement_date=date.fromisoformat(row["statement_date"]),
        total_value=row["total_value"],
        holding_count=row["holding_count"],
        source_filename=row["source_filename"],
    )
