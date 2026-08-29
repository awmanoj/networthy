"""AIF / VC / PE units that arrive through a statement.

SEBI mandated demat for AIF units, so an angel, venture or private-equity
commitment shows up in an NSDL CAS next to the liquid holdings — carrying an ISIN
that looks exactly like a mutual fund's (INF) or a share's (INE). Only the name
separates them, and getting it wrong is worse than cosmetic: the commitment lands
in Mutual Funds looking redeemable, and if the holder also recorded it by hand
under Alternate Investments it counts twice in net worth, from two leaves they'd
never see side by side.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage
from app.models import Account, Holding, Snapshot


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    import app.main as m
    return TestClient(m.app)


def _login(email="aif@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _cas_with_aif(uid):
    """A snapshot holding one AIF unit and one ordinary mutual fund."""
    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=3_000_000.0,
        holding_count=2, source_filename="cas.pdf",
    ))
    storage.replace_holdings(sid, [Account(kind="demat", name="NSDL", holdings=[
        Holding("ABC INDIA GROWTH FUND AIF CATEGORY II", "private_equity",
                "INF1234567890", 1000.0, 2000.0, 2_000_000.0),
        Holding("HDFC FLEXI CAP FUND - DIRECT GROWTH", "mutual_fund",
                "INF9876543210", 500.0, 2000.0, 1_000_000.0),
    ])])


def test_aif_from_a_cas_lands_in_alternate_investments_not_mutual_funds(client):
    """The point of the classification fix: an illiquid AIF commitment must not
    sit in the Mutual Funds leaf looking like something you can redeem."""
    uid, ck = _login()
    _cas_with_aif(uid)
    import app.main as m

    class U:  # the shape _leaf_value expects
        id = uid

    alt = m._leaf_value(U, m.ALT_LEAF)
    assert alt == 2_000_000.0, "AIF units missing from Alternate Investments"

    mf = m._leaf_value(U, "mutual-funds")
    assert mf == 1_000_000.0, "the AIF leaked into Mutual Funds"


def test_the_aif_shows_on_the_leaf_page_with_a_duplicate_warning(client):
    uid, ck = _login()
    _cas_with_aif(uid)
    page = client.get("/networth/assets/financial-assets/alternate-investments",
                      cookies=ck).text
    assert "ABC INDIA GROWTH FUND" in page
    assert "From your statements" in page
    # The warning is the point: someone who also entered this by hand needs to
    # see that it would count twice.
    assert "counts twice" in page


def test_hand_entered_and_cas_alt_investments_are_summed_not_replaced(client):
    uid, ck = _login()
    _cas_with_aif(uid)
    storage.add_alt_investment(uid, name="Angel — Acme", current_value=500_000.0)
    import app.main as m

    class U:
        id = uid

    assert m._leaf_value(U, m.ALT_LEAF) == 2_500_000.0
