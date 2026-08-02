"""Small direct-equity holdings (< MIN_EQUITY_VALUE) are tracking-only and are
dropped from the Equity leaf and net worth."""

from datetime import date

import pytest

from app import main, prices, storage
from app.models import Account, Holding, Snapshot


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    # No live pricing in this test — value falls back to statement value.
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    return storage


def _seed_equity(db):
    uid = db.get_or_create_user("a@b.com").id
    sid = db.upsert_snapshot(
        uid, Snapshot(statement_date=date(2026, 6, 30), total_value=50500, holding_count=2)
    )
    account = Account(kind="demat", name="Zerodha", holdings=[
        Holding(name="BigCo", asset_class="direct_equity", isin="INE001A01011",
                units=10, price=5000.0, value=50000.0),
        Holding(name="TinyCo", asset_class="direct_equity", isin="INE002A01012",
                units=1, price=500.0, value=500.0),   # tracking-only, < 10k
    ])
    db.replace_holdings(sid, account.holdings and [account])
    return db.get_user(uid)


def test_tiny_equity_excluded_from_leaf_value_and_holdings(db):
    user = _seed_equity(db)
    # Only BigCo counts toward the Equity leaf / net worth.
    assert main._leaf_value(user, "equity") == pytest.approx(50000.0)
    leaf = main._leaf_holdings(user, "equity")
    assert [h["name"] for h in leaf["holdings"]] == ["BigCo"]


def test_threshold_is_ten_thousand(db):
    # A holding exactly at the threshold is kept; just under is dropped.
    uid = db.get_or_create_user("c@d.com").id
    sid = db.upsert_snapshot(
        uid, Snapshot(statement_date=date(2026, 6, 30), total_value=19999, holding_count=2)
    )
    rows = [
        Holding(name="AtThreshold", asset_class="direct_equity", isin="INE1", value=10000.0),
        Holding(name="Under", asset_class="direct_equity", isin="INE2", value=9999.0),
    ]
    db.replace_holdings(sid, [Account(kind="demat", name="Z", holdings=rows)])
    leaf = main._leaf_holdings(db.get_user(uid), "equity")
    assert [h["name"] for h in leaf["holdings"]] == ["AtThreshold"]
