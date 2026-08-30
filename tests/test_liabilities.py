"""Tests for liability enrichment (% paid off, remaining tenure) and net-worth sign."""

from datetime import date, timedelta

from app.main import _enrich_liability, _property_share
from app.networth import LIABILITY_LEAVES, resolve


def test_property_share_attribution():
    # Joint 50% of a 1.2 Cr flat -> 60 L counts toward net worth.
    assert _property_share({"current_value": 12000000.0, "share_pct": 50.0}) == 6000000.0
    # None share means 100%.
    assert _property_share({"current_value": 4000000.0, "share_pct": None}) == 4000000.0


def test_liability_leaves_match_the_tree():
    """Every leaf that subtracts from net worth is reachable, and nothing that
    isn't a leaf sneaks into the set."""
    secured = {c.slug for c in resolve("liabilities/secured-loans")[-1].children}
    unsecured = {c.slug for c in resolve("liabilities/unsecured-loans")[-1].children}
    adjustments = {c.slug for c in resolve("liabilities/adjustments")[-1].children}
    assert LIABILITY_LEAVES == secured | unsecured | adjustments


def test_paid_off_from_principal_and_outstanding():
    rows = [{"principal": 5000000.0, "outstanding": 3500000.0, "end_date": None}]
    _enrich_liability(rows)
    assert rows[0]["paid_pct"] == 30.0   # (5,000,000 - 3,500,000)/5,000,000


def test_no_principal_means_no_paid_off():
    rows = [{"principal": None, "outstanding": 45000.0, "end_date": None}]
    _enrich_liability(rows)
    assert rows[0]["paid_pct"] is None


def test_remaining_tenure_from_end_date():
    future = (date.today() + timedelta(days=800)).isoformat()
    past = (date.today() - timedelta(days=10)).isoformat()
    rows = [
        {"principal": None, "outstanding": 1.0, "end_date": future},
        {"principal": None, "outstanding": 1.0, "end_date": past},
    ]
    _enrich_liability(rows)
    assert rows[0]["remaining"].endswith("yrs left")
    assert rows[1]["remaining"] == "ended"


# --- Valuation adjustment ----------------------------------------------------
#
# A catch-all write-down for assets you don't want counted at face value: an AIF
# marked at ₹4 cr you'd rather carry at ₹2.5 cr. It shares the liabilities table
# and the subtract-from-net-worth behaviour, but it isn't debt.

def test_an_adjustment_reduces_net_worth(tmp_path, monkeypatch):
    from app import networth, prices, storage
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    import app.main as m

    user = storage.get_or_create_user("adj@test.com")
    storage.add_alt_investment(user.id, name="Some AIF", current_value=40_000_000.0)
    assert m._dashboard(user)["net_worth"] == 40_000_000.0

    # Carry it at ₹2.5 cr instead: a ₹1.5 cr discount.
    storage.add_liability(user.id, networth.ADJUSTMENT_LEAF,
                          lender="AIF carried below stated value",
                          outstanding=15_000_000.0)
    d = m._dashboard(user)
    assert d["liabilities"] == 15_000_000.0
    assert d["net_worth"] == 25_000_000.0
    # The asset keeps its stated value — the discount is visible, not baked in.
    assert d["assets"] == 40_000_000.0


def test_the_adjustment_page_asks_for_a_discount_not_a_lender(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from app import auth, prices, storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    import app.main as m

    uid = storage.get_or_create_user("adjui@test.com").id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    page = TestClient(m.app).get(
        "/networth/liabilities/adjustments/valuation-adjustment",
        cookies={auth.SESSION_COOKIE: "tok"}).text

    assert "What you're discounting" in page
    assert "Discount ₹" in page
    # None of the loan machinery belongs on a write-down.
    for absent in ("Monthly EMI", "Rate %", "Ends on", "Borrowed ₹"):
        assert absent not in page, f"{absent} should not appear on the adjustment page"
