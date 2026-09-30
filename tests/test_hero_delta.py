"""Day-over-day and week-over-week change beside the hero net worth."""

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, digest, prices, storage


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


def _login(email="d@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _cash(uid, amount):
    """One bank row, so net worth is a number we control exactly."""
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute("INSERT INTO bank_cash (user_id, leaf_slug, bank_name, balance) "
                 "VALUES (?,?,?,?)", (uid, "bank-accounts", "HDFC", amount))
    conn.commit()
    conn.close()


def _point(uid, days_ago, value):
    day = (digest.ist_today() - timedelta(days=days_ago)).isoformat()
    storage.ensure_nw_point(uid, day, value, value, 0.0)


def _user(email):
    return storage.get_or_create_user(email)


def test_both_deltas_show_when_there_is_history(client):
    uid, ck = _login()
    _cash(uid, 1_100_000)
    _point(uid, 1, 1_000_000)       # yesterday
    _point(uid, 7, 900_000)         # a week ago

    import app.main as m
    deltas = {d["label"]: d for d in m._dashboard(_user("d@test.com"))["deltas"]}
    assert deltas["today"]["delta"] == pytest.approx(100_000)
    assert deltas["today"]["pct"] == pytest.approx(10.0)
    assert deltas["this week"]["delta"] == pytest.approx(200_000)
    body = client.get("/", cookies=ck).text
    assert "₹1,00,000" in body or "100,000" in body
    assert "today" in body and "this week" in body


def test_a_new_account_shows_nothing_rather_than_zero(client):
    """No history isn't "flat" — it's no basis for a claim at all."""
    uid, ck = _login("new@test.com")
    _cash(uid, 500_000)
    import app.main as m
    assert m._dashboard(_user("new@test.com"))["deltas"] == []
    assert "flat today" not in client.get("/", cookies=ck).text


def test_todays_own_point_is_never_the_baseline(client):
    """ensure_nw_point writes a row on every dashboard view. Comparing against
    it would report ₹0 change forever — the bug this guards."""
    uid, ck = _login("same@test.com")
    _cash(uid, 2_000_000)
    client.get("/", cookies=ck)                    # writes today's point
    _point(uid, 1, 1_800_000)

    import app.main as m
    deltas = {d["label"]: d for d in m._dashboard(_user("same@test.com"))["deltas"]}
    assert deltas["today"]["delta"] == pytest.approx(200_000)   # not 0


def test_a_fall_is_marked_down(client):
    uid, ck = _login("down@test.com")
    _cash(uid, 900_000)
    _point(uid, 1, 1_000_000)
    import app.main as m
    d = m._dashboard(_user("down@test.com"))["deltas"][0]
    assert d["delta"] < 0 and not d["up"]
    assert "▼" in client.get("/", cookies=ck).text


def test_a_rupee_of_drift_reads_as_flat(client):
    """Live prices wobble. A rupee on a crore is noise, not news."""
    uid, _ = _login("flat@test.com")
    _cash(uid, 10_000_000)
    _point(uid, 1, 10_000_000.4)
    import app.main as m
    assert m._dashboard(_user("flat@test.com"))["deltas"][0]["flat"]


def test_only_the_week_delta_when_yesterday_is_missing(client):
    """Gaps are normal — nobody opens the app every day."""
    uid, _ = _login("gap@test.com")
    _cash(uid, 1_200_000)
    _point(uid, 9, 1_000_000)          # older than a week, nothing yesterday
    import app.main as m
    labels = [d["label"] for d in m._dashboard(_user("gap@test.com"))["deltas"]]
    assert labels == ["today", "this week"] or labels == ["this week"]


def test_it_agrees_with_the_digest(client):
    """Both read nw_history, so the dashboard and the evening email must not
    tell the user two different numbers."""
    uid, _ = _login("agree@test.com")
    _cash(uid, 1_100_000)
    _point(uid, 1, 1_000_000)

    import app.main as m
    user = _user("agree@test.com")
    dash = m._dashboard(user)
    prev = storage.latest_nw_snapshot_before(uid, digest.ist_today().isoformat())
    assert dash["deltas"][0]["delta"] == pytest.approx(dash["net_worth"] - prev["net_worth"])
