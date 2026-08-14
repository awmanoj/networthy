"""Tests for expense annualisation and the FIRE/runway maths."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, expenses, prices, storage


@pytest.mark.parametrize(
    "amount,count,freq,expected",
    [
        (1000.0, 1, "monthly", 12000.0),
        (30000.0, 1, "quarterly", 120000.0),
        (25000.0, 1, "half-yearly", 50000.0),
        (100000.0, 1, "annual", 100000.0),
        (100000.0, 2, "annual", 200000.0),   # count scales (2 kids)
        (5000.0, 4, "monthly", 240000.0),      # 4 members × 5k/mo
    ],
)
def test_annual_amount(amount, count, freq, expected):
    assert expenses.annual_amount(amount, count, freq) == pytest.approx(expected)


def test_annual_amount_unknown_frequency_is_zero():
    assert expenses.annual_amount(1000.0, 1, "weekly") == 0.0


def test_category_and_frequency_lookups():
    assert expenses.category_label("housing") == "Housing"
    assert expenses.category_label("bogus") == "bogus"
    assert expenses.category_color("food").startswith("#")
    assert expenses.frequency_label("half-yearly") == "Half-yearly"
    assert {c["slug"] for c in expenses.CATEGORIES} >= {"housing", "food", "other"}


def test_fire_maths_uses_the_withdrawal_rate():
    # Target = annual burn ÷ withdrawal rate; runway = net worth / annual.
    annual = 1200000.0
    net_worth = 15000000.0
    assert expenses.fire_target(annual, 4.0) == pytest.approx(30000000.0)   # 25×
    assert expenses.fire_target(annual, 3.0) == pytest.approx(40000000.0)   # 33.3×
    assert expenses.fire_target(annual, 2.5) == pytest.approx(48000000.0)   # 40×
    assert net_worth / annual == pytest.approx(12.5)   # 12.5 years of runway


def test_swr_default_is_india_realistic_not_the_us_4_percent():
    assert expenses.DEFAULT_SWR_PCT == 3.0
    assert expenses.normalise_swr(None) == 3.0
    assert expenses.swr_multiple(None) == pytest.approx(100 / 3.0)
    # The 4% rule is still offered, just not the default.
    assert 4.0 in {p["pct"] for p in expenses.SWR_PRESETS}
    assert 2.5 in {p["pct"] for p in expenses.SWR_PRESETS}


@pytest.mark.parametrize(
    "given,expected",
    [
        (2.5, 2.5),
        (0, expenses.DEFAULT_SWR_PCT),        # 0% would mean an infinite corpus
        (-1, expenses.DEFAULT_SWR_PCT),
        (None, expenses.DEFAULT_SWR_PCT),
        ("", expenses.DEFAULT_SWR_PCT),
        (0.2, expenses.SWR_MIN_PCT),          # clamped up
        (99.0, expenses.SWR_MAX_PCT),         # clamped down
    ],
)
def test_normalise_swr(given, expected):
    assert expenses.normalise_swr(given) == pytest.approx(expected)


def test_swr_multiple_is_the_inverse_of_the_rate():
    assert expenses.swr_multiple(4.0) == pytest.approx(25.0)
    assert expenses.swr_multiple(2.5) == pytest.approx(40.0)


# --- The per-user withdrawal rate, end to end --------------------------------

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


def _login(email="swr@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def test_swr_storage_defaults_to_none_and_upserts(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    a = storage.get_or_create_user("a@b.com").id
    b = storage.get_or_create_user("b@b.com").id

    assert storage.get_swr_pct(a) is None            # unset -> the module default applies
    storage.save_swr_pct(a, 2.5)
    storage.save_swr_pct(a, 3.5)                     # upsert, one row
    assert storage.get_swr_pct(a) == pytest.approx(3.5)
    assert storage.get_swr_pct(b) is None            # per-user, untouched

    # Saving a rate must not disturb the CAMS PAN/email in the same row (or vice versa).
    storage.save_user_settings(a, "ABCDE1234F", "reg@cams.com")
    assert storage.get_swr_pct(a) == pytest.approx(3.5)
    storage.save_swr_pct(a, 2.5)
    assert storage.get_user_settings(a) == {"pan": "ABCDE1234F", "cams_email": "reg@cams.com"}


def test_expenses_page_shows_the_rate_and_the_ladder(client):
    uid, ck = _login()
    storage.add_expense(uid, "Rent", "housing", 100000.0, "monthly")

    page = client.get("/expenses", cookies=ck).text
    assert "3% SWR (33×)" in page                     # default, not the US 4% rule
    assert "₹40,000,000" in page                      # 12L/yr ÷ 3% = 4 crore
    assert "US · Trinity rule" in page                # the 4% option is still offered
    assert "₹30,000,000" in page                      # ...at 25× on the ladder

    r = client.post("/expenses/swr", data={"swr_pct": "2.5"},
                    cookies=ck, follow_redirects=False)
    assert r.status_code == 303
    assert storage.get_swr_pct(uid) == pytest.approx(2.5)

    page = client.get("/expenses", cookies=ck).text
    assert "2.5% SWR (40×)" in page
    assert "₹48,000,000" in page                      # 12L/yr ÷ 2.5%


def test_goals_fire_mirror_follows_the_same_rate(client):
    uid, ck = _login("mirror@test.com")
    storage.add_expense(uid, "Rent", "housing", 100000.0, "monthly")
    storage.save_swr_pct(uid, 2.5)

    page = client.get("/goals", cookies=ck).text
    assert "40× your annual expenses · a 2.5% safe-withdrawal corpus" in page
    assert "₹48,000,000" in page


def test_out_of_range_rate_is_clamped_not_stored_raw(client):
    uid, ck = _login("clamp@test.com")
    client.post("/expenses/swr", data={"swr_pct": "0.1"}, cookies=ck, follow_redirects=False)
    assert storage.get_swr_pct(uid) == pytest.approx(expenses.SWR_MIN_PCT)
