"""SQLite persistence for net-worth snapshots.

Uses the stdlib sqlite3 driver to keep dependencies minimal. The database lives
under data/ which is gitignored — parsed financial data never leaves the machine.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .models import Snapshot

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "networthy.db"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they do not exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                statement_date  TEXT NOT NULL UNIQUE,
                total_value     REAL NOT NULL,
                holding_count   INTEGER NOT NULL DEFAULT 0,
                source_filename TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


def upsert_snapshot(snapshot: Snapshot) -> None:
    """Insert a snapshot, replacing any existing one for the same statement date.

    A given CAS date maps to exactly one net-worth figure, so re-uploading the
    same statement should overwrite rather than duplicate.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO snapshots
                (statement_date, total_value, holding_count, source_filename)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(statement_date) DO UPDATE SET
                total_value     = excluded.total_value,
                holding_count   = excluded.holding_count,
                source_filename = excluded.source_filename
            """,
            (
                snapshot.statement_date.isoformat(),
                snapshot.total_value,
                snapshot.holding_count,
                snapshot.source_filename,
            ),
        )


def list_snapshots() -> list[Snapshot]:
    """Return all snapshots ordered oldest-first (chart-ready)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots ORDER BY statement_date ASC"
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def delete_snapshot(snapshot_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


def delete_all_snapshots() -> None:
    """Remove every snapshot, returning the dashboard to its empty state."""
    with _connect() as conn:
        conn.execute("DELETE FROM snapshots")


def _row_to_snapshot(row: sqlite3.Row) -> Snapshot:
    return Snapshot(
        id=row["id"],
        statement_date=date.fromisoformat(row["statement_date"]),
        total_value=row["total_value"],
        holding_count=row["holding_count"],
        source_filename=row["source_filename"],
    )
