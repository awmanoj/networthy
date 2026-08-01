"""Tests for multi-user data isolation and the legacy-data migration.

storage uses a module-level DB_PATH; each test points it at a temp file so runs
are isolated and never touch the real data/ database.
"""

from datetime import date

import pytest

from app import storage
from app.models import Account, Holding, Snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    return storage


def _snap(day: int, value: float) -> Snapshot:
    return Snapshot(
        statement_date=date(2024, 1, day),
        total_value=value,
        holding_count=1,
        source_filename=f"cas_{day}.pdf",
    )


def test_snapshots_are_isolated_per_user(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id

    db.upsert_snapshot(alice, _snap(1, 100.0))
    db.upsert_snapshot(alice, _snap(2, 200.0))
    db.upsert_snapshot(bob, _snap(1, 999.0))

    alice_snaps = db.list_snapshots(alice)
    bob_snaps = db.list_snapshots(bob)

    assert [s.total_value for s in alice_snaps] == [100.0, 200.0]
    assert [s.total_value for s in bob_snaps] == [999.0]


def test_same_date_allowed_across_users(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    # Same statement_date must not collide across accounts.
    db.upsert_snapshot(alice, _snap(1, 100.0))
    db.upsert_snapshot(bob, _snap(1, 200.0))
    assert len(db.list_snapshots(alice)) == 1
    assert len(db.list_snapshots(bob)) == 1


def test_upsert_replaces_same_date_for_one_user(db):
    alice = db.get_or_create_user("alice@example.com").id
    db.upsert_snapshot(alice, _snap(1, 100.0))
    db.upsert_snapshot(alice, _snap(1, 150.0))  # same date, new value
    snaps = db.list_snapshots(alice)
    assert len(snaps) == 1 and snaps[0].total_value == 150.0


def test_delete_is_scoped_to_owner(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.upsert_snapshot(bob, _snap(1, 999.0))
    (bob_snap,) = db.list_snapshots(bob)

    # Alice attempting to delete Bob's row by id must be a no-op.
    db.delete_snapshot(alice, bob_snap.id)
    assert len(db.list_snapshots(bob)) == 1

    db.delete_snapshot(bob, bob_snap.id)
    assert db.list_snapshots(bob) == []


def _accounts() -> list[Account]:
    return [
        Account(
            kind="demat", name="ZERODHA", identifier="12081600 / 999", depository="NSDL",
            holdings=[
                Holding(name="INFOSYS", asset_class="direct_equity", isin="INE009A01021",
                        units=100, price=1500.0, value=150000.0),
                Holding(name="HDFC BANK", asset_class="direct_equity", isin="INE040A01034",
                        units=50, price=1600.5, value=80025.0),
            ],
        ),
        Account(
            kind="mutual_fund", name="HDFC MF", identifier="1234567/89",
            holdings=[
                Holding(name="Balanced Adv", asset_class="mutual_fund",
                        isin="INF179K01BE2", units=500.123, price=45.67, value=22842.11),
            ],
        ),
    ]


def test_holdings_round_trip_preserves_accounts_and_order(db):
    alice = db.get_or_create_user("alice@example.com").id
    sid = db.upsert_snapshot(alice, _snap(1, 252867.11))
    db.replace_holdings(sid, _accounts())

    accounts = db.list_accounts(sid)
    assert [a.name for a in accounts] == ["ZERODHA", "HDFC MF"]
    demat = accounts[0]
    assert demat.kind == "demat" and demat.depository == "NSDL"
    assert [h.name for h in demat.holdings] == ["INFOSYS", "HDFC BANK"]  # order kept
    assert demat.value == pytest.approx(230025.0)
    assert accounts[1].holdings[0].asset_class == "mutual_fund"


def test_replace_holdings_is_idempotent(db):
    """Re-parsing a statement rebuilds its rows rather than accumulating them."""
    alice = db.get_or_create_user("alice@example.com").id
    sid = db.upsert_snapshot(alice, _snap(1, 100.0))
    db.replace_holdings(sid, _accounts())
    db.replace_holdings(sid, _accounts())  # upload the same file again
    accounts = db.list_accounts(sid)
    assert sum(len(a.holdings) for a in accounts) == 3


def test_deleting_snapshot_cascades_to_holdings(db):
    alice = db.get_or_create_user("alice@example.com").id
    sid = db.upsert_snapshot(alice, _snap(1, 100.0))
    db.replace_holdings(sid, _accounts())
    db.delete_snapshot(alice, sid)
    assert db.list_accounts(sid) == []


def test_latest_snapshot_returns_most_recent(db):
    alice = db.get_or_create_user("alice@example.com").id
    db.upsert_snapshot(alice, _snap(1, 100.0))
    db.upsert_snapshot(alice, _snap(5, 200.0))
    assert db.latest_snapshot(alice).total_value == 200.0
    assert db.latest_snapshot(db.get_or_create_user("bob@example.com").id) is None


def _mf(name, isin, value, asset_class="mutual_fund") -> Holding:
    return Holding(name=name, asset_class=asset_class, isin=isin,
                   units=100.0, price=value / 100.0, value=value)


def test_networth_import_round_trip_and_replace(db):
    alice = db.get_or_create_user("alice@example.com").id
    db.replace_networth_import(alice, "cams", date(2024, 6, 30), [
        _mf("HDFC Flexi Cap", "INF179K01BE2", 100000.0),
        _mf("Nippon Gold Fund", "INF204KA1UB1", 50000.0, "gold"),
    ])
    mfs = db.list_networth_holdings(alice, {"mutual_fund"})
    assert [h["name"] for h in mfs] == ["HDFC Flexi Cap"]
    assert mfs[0]["source"] == "cams" and mfs[0]["as_of_date"] == "2024-06-30"

    gold = db.list_networth_holdings(alice, {"gold", "silver"})
    assert [h["name"] for h in gold] == ["Nippon Gold Fund"]

    # Re-import replaces wholesale (no accumulation).
    db.replace_networth_import(alice, "cams", date(2024, 7, 31), [
        _mf("HDFC Flexi Cap", "INF179K01BE2", 120000.0),
    ])
    mfs = db.list_networth_holdings(alice, {"mutual_fund"})
    assert len(mfs) == 1 and mfs[0]["value"] == 120000.0
    assert db.list_networth_holdings(alice, {"gold", "silver"}) == []


def test_networth_import_is_user_scoped(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.replace_networth_import(alice, "cams", date(2024, 6, 30),
                               [_mf("HDFC Flexi Cap", "INF179K01BE2", 100000.0)])
    assert db.list_networth_holdings(bob, {"mutual_fund"}) == []


def test_latest_holdings_by_class_reads_from_latest_snapshot(db):
    alice = db.get_or_create_user("alice@example.com").id
    sid = db.upsert_snapshot(alice, _snap(1, 252867.11))
    db.replace_holdings(sid, _accounts())  # has equities + one MF
    mfs = db.latest_holdings_by_class(alice, {"mutual_fund"})
    assert [h["name"] for h in mfs] == ["Balanced Adv"]
    assert mfs[0]["source"] == "nsdl"
    # Classes with no matching holdings return nothing.
    assert db.latest_holdings_by_class(alice, {"gold"}) == []


def test_latest_holdings_by_class_skips_value_less_rows(db):
    # Blank ISIN rows a prior parse may have stored (value=None) are hidden.
    alice = db.get_or_create_user("alice@example.com").id
    sid = db.upsert_snapshot(alice, _snap(1, 100.0))
    db.replace_holdings(sid, [
        Account(kind="mutual_fund", name="HDFC MF", holdings=[
            Holding(name="Real Fund", asset_class="mutual_fund", isin="INF1",
                    units=10, price=10.0, value=100.0),
            Holding(name="Blank Row", asset_class="mutual_fund", isin="INF2",
                    units=None, price=None, value=None),
        ]),
    ])
    mfs = db.latest_holdings_by_class(alice, {"mutual_fund"})
    assert [h["name"] for h in mfs] == ["Real Fund"]


def test_manual_holdings_crud_round_trip(db):
    alice = db.get_or_create_user("alice@example.com").id
    db.add_manual_holding(
        alice, "ppf", "SBI PPF", 500000.0,
        maturity_amount=1200000.0, rate=7.1,
        investment_date="2023-04-01", maturity_date="2038-04-01",
    )
    db.add_manual_holding(alice, "ppf", "HDFC PPF", 100000.0)  # optional fields None
    db.add_manual_holding(alice, "nsc", "NSC VIII", 50000.0)   # different leaf

    ppf = db.list_manual_holdings(alice, "ppf")
    assert [m["scheme"] for m in ppf] == ["SBI PPF", "HDFC PPF"]  # entry order
    assert ppf[0]["investment_amount"] == 500000.0
    assert ppf[0]["maturity_amount"] == 1200000.0
    assert ppf[0]["rate"] == 7.1
    assert ppf[0]["investment_date"] == "2023-04-01"
    assert ppf[0]["maturity_date"] == "2038-04-01"
    assert ppf[1]["maturity_amount"] is None and ppf[1]["investment_date"] is None

    # Leaf scoping: nsc entry doesn't leak into ppf.
    assert [m["scheme"] for m in db.list_manual_holdings(alice, "nsc")] == ["NSC VIII"]


def test_manual_holdings_delete_is_owner_scoped(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.add_manual_holding(bob, "ppf", "Bob PPF", 10000.0)
    (row,) = db.list_manual_holdings(bob, "ppf")

    db.delete_manual_holding(alice, row["id"])           # not Bob's — no-op
    assert len(db.list_manual_holdings(bob, "ppf")) == 1
    db.delete_manual_holding(bob, row["id"])
    assert db.list_manual_holdings(bob, "ppf") == []


def test_manual_holdings_cascade_on_user_delete(db):
    import sqlite3
    alice = db.get_or_create_user("alice@example.com").id
    db.add_manual_holding(alice, "ppf", "SBI PPF", 500000.0)
    with db._connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (alice,))
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM manual_holdings WHERE user_id = ?", (alice,)
        ).fetchone()
    assert n == 0  # ON DELETE CASCADE


def test_foreign_holdings_crud(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.add_foreign_holding(alice, "AAPL", 10, 300.0)
    db.add_foreign_holding(alice, "MSFT", 5)          # no cost
    db.add_foreign_holding(bob, "NVDA", 3, 100.0)     # different owner

    rows = db.list_foreign_holdings(alice)
    assert [r["ticker"] for r in rows] == ["AAPL", "MSFT"]  # entry order
    assert rows[0]["units"] == 10 and rows[0]["cost_usd"] == 300.0
    assert rows[1]["cost_usd"] is None

    # Owner scoping on delete.
    db.delete_foreign_holding(bob, rows[0]["id"])          # not Bob's -> no-op
    assert len(db.list_foreign_holdings(alice)) == 2
    db.delete_foreign_holding(alice, rows[0]["id"])
    assert [r["ticker"] for r in db.list_foreign_holdings(alice)] == ["MSFT"]


def test_bank_cash_crud(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.add_bank_cash(alice, "bank-accounts", 250000.0,
                     bank_name="HDFC Bank", account_type="Savings", label="Salary")
    db.add_bank_cash(alice, "bank-accounts", 40000.0, bank_name="SBI", account_type="Current")
    db.add_bank_cash(alice, "cash", 5000.0, label="Cash at home")   # different leaf
    db.add_bank_cash(bob, "bank-accounts", 999.0, bank_name="ICICI")

    banks = db.list_bank_cash(alice, "bank-accounts")
    assert [b["bank_name"] for b in banks] == ["HDFC Bank", "SBI"]  # entry order
    assert banks[0]["account_type"] == "Savings" and banks[0]["label"] == "Salary"
    assert banks[0]["balance"] == 250000.0

    # Leaf scoping: cash entry doesn't show under bank-accounts.
    cash = db.list_bank_cash(alice, "cash")
    assert [c["label"] for c in cash] == ["Cash at home"] and cash[0]["balance"] == 5000.0

    # Owner-scoped delete.
    db.delete_bank_cash(bob, banks[0]["id"])         # not Bob's -> no-op
    assert len(db.list_bank_cash(alice, "bank-accounts")) == 2
    db.delete_bank_cash(alice, banks[0]["id"])
    assert [b["bank_name"] for b in db.list_bank_cash(alice, "bank-accounts")] == ["SBI"]


def test_forex_holdings_crud(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.add_forex_holding(alice, "USD", 5000.0, kind="Account", label="Wise")
    db.add_forex_holding(alice, "EUR", 800.0, kind="Cash")
    db.add_forex_holding(bob, "GBP", 200.0)

    rows = db.list_forex_holdings(alice)
    assert [r["currency"] for r in rows] == ["USD", "EUR"]  # entry order
    assert rows[0]["amount"] == 5000.0 and rows[0]["kind"] == "Account" and rows[0]["label"] == "Wise"
    assert rows[1]["label"] is None

    db.delete_forex_holding(bob, rows[0]["id"])            # not Bob's -> no-op
    assert len(db.list_forex_holdings(alice)) == 2
    db.delete_forex_holding(alice, rows[0]["id"])
    assert [r["currency"] for r in db.list_forex_holdings(alice)] == ["EUR"]


def test_alt_investments_crud(db):
    alice = db.get_or_create_user("alice@example.com").id
    bob = db.get_or_create_user("bob@example.com").id
    db.add_alt_investment(alice, "Acme Robotics", 2500000.0,
                          category="Startup / Angel", cost=500000.0, invested_date="2022-04-01")
    db.add_alt_investment(alice, "MyCo ESOP", 800000.0)  # optional fields None
    db.add_alt_investment(bob, "Other", 111.0)

    rows = db.list_alt_investments(alice)
    assert [r["name"] for r in rows] == ["Acme Robotics", "MyCo ESOP"]  # entry order
    assert rows[0]["cost"] == 500000.0 and rows[0]["current_value"] == 2500000.0
    assert rows[0]["category"] == "Startup / Angel" and rows[0]["invested_date"] == "2022-04-01"
    assert rows[1]["cost"] is None and rows[1]["category"] is None

    db.delete_alt_investment(bob, rows[0]["id"])          # not Bob's -> no-op
    assert len(db.list_alt_investments(alice)) == 2
    db.delete_alt_investment(alice, rows[0]["id"])
    assert [r["name"] for r in db.list_alt_investments(alice)] == ["MyCo ESOP"]


def test_get_or_create_user_is_idempotent_and_normalizes(db):
    a = db.get_or_create_user("Alice@Example.com ")
    b = db.get_or_create_user("alice@example.com")
    assert a.id == b.id
    assert a.email == "alice@example.com"


def test_legacy_snapshots_migrate_to_owner(tmp_path, monkeypatch):
    """A pre-multi-user DB (global snapshots, no user_id) migrates on init_db."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # Build the *old* schema and seed rows.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_date TEXT NOT NULL UNIQUE,
            total_value REAL NOT NULL,
            holding_count INTEGER NOT NULL DEFAULT 0,
            source_filename TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO snapshots (statement_date, total_value, holding_count) VALUES (?,?,?)",
        ("2023-06-30", 500000.0, 42),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")

    storage.init_db()

    owner = storage.get_or_create_user("owner@example.com")
    snaps = storage.list_snapshots(owner.id)
    assert len(snaps) == 1
    assert snaps[0].total_value == 500000.0
    assert snaps[0].user_id == owner.id
