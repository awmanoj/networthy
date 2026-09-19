"""Surfacing hand-entered figures that have gone stale.

A net worth built partly from live prices and partly from a number typed 14
months ago presents both with identical confidence. This is what tells them
apart — and what turns "go update everything" into a short, finite list.
"""

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
    monkeypatch.setattr(prices, "gold_inr_per_gram", lambda: 7000.0)
    monkeypatch.setattr(prices, "crypto_inr", lambda s: None)
    import app.main as m
    return TestClient(m.app)


def _login(email="stale@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _insert(sql, args, age_days):
    """Insert a row with both timestamps set `age_days` in the past."""
    conn = sqlite3.connect(storage.DB_PATH)
    conn.execute(sql.replace("AGE", f"'-{age_days} days'"), args)
    conn.commit()
    conn.close()


def _ppf(uid, age_days, amount=900000.0, name="PPF SBI"):
    _insert("INSERT INTO manual_holdings (user_id, leaf_slug, scheme, investment_amount,"
            " created_at, updated_at) VALUES (?,?,?,?,datetime('now',AGE),datetime('now',AGE))",
            (uid, "ppf", name, amount), age_days)


# --- What counts as stale -----------------------------------------------------

def test_old_hand_entered_rows_are_listed_oldest_first(client):
    uid, _ = _login()
    _ppf(uid, 400, name="PPF SBI")
    _insert("INSERT INTO liabilities (user_id, leaf_slug, lender, outstanding,"
            " created_at, updated_at) VALUES (?,?,?,?,datetime('now',AGE),datetime('now',AGE))",
            (uid, "home-loan", "HDFC", 3700000.0), 300)
    rows = storage.stale_entries(uid)
    assert [r["label"] for r in rows] == ["PPF SBI", "HDFC"]


def test_recent_rows_are_not_listed(client):
    uid, _ = _login("recent@test.com")
    _ppf(uid, 10)
    assert storage.stale_entries(uid) == []


def test_live_priced_rows_never_go_stale(client):
    """Crypto, US equity and forex are fetched, so how long ago they were typed
    says nothing about whether the value is right."""
    uid, _ = _login("live@test.com")
    for sql, args in (
        ("INSERT INTO crypto_holdings (user_id, symbol, quantity, created_at)"
         " VALUES (?,?,?,datetime('now',AGE))", (uid, "BTC", 0.5)),
        ("INSERT INTO foreign_holdings (user_id, ticker, units, created_at)"
         " VALUES (?,?,?,datetime('now',AGE))", (uid, "AAPL", 10.0)),
        ("INSERT INTO forex_holdings (user_id, currency, amount, created_at)"
         " VALUES (?,?,?,datetime('now',AGE))", (uid, "USD", 5000.0)),
    ):
        _insert(sql, args, 900)
    assert storage.stale_entries(uid) == []


def test_gold_by_weight_is_live_but_gold_by_value_is_not(client):
    """Same table, two kinds of row: weight + karat is priced from the gold rate,
    a flat figure is someone's memory."""
    uid, _ = _login("gold@test.com")
    _insert("INSERT INTO gold_items (user_id, description, weight_g, karat, created_at,"
            " updated_at) VALUES (?,?,?,?,datetime('now',AGE),datetime('now',AGE))",
            (uid, "Bangles", 120.0, 22), 900)
    _insert("INSERT INTO gold_items (user_id, description, flat_value, created_at,"
            " updated_at) VALUES (?,?,?,datetime('now',AGE),datetime('now',AGE))",
            (uid, "Coins", 250000.0), 900)
    assert [r["label"] for r in storage.stale_entries(uid)] == ["Coins"]


# --- Clearing it --------------------------------------------------------------

def test_editing_a_row_makes_it_current_again(client):
    uid, _ = _login("edit@test.com")
    _ppf(uid, 400)
    row_id = storage.stale_entries(uid)[0]["id"]
    storage.update_row("manual_holdings", row_id, uid, investment_amount=950000.0)
    assert storage.stale_entries(uid) == []


def test_still_right_resets_the_clock_without_changing_the_value(client):
    """Without this, the only way to clear a stale flag is to retype a number you
    haven't verified — exactly the habit the feature exists to discourage."""
    uid, ck = _login("touch@test.com")
    _ppf(uid, 400, amount=900000.0)
    row = storage.stale_entries(uid)[0]

    r = client.post("/networth/touch",
                    data={"source": row["source"], "id": row["id"], "redirect": "/"},
                    cookies=ck, follow_redirects=False)
    assert r.status_code == 303
    assert storage.stale_entries(uid) == []

    conn = sqlite3.connect(storage.DB_PATH)
    (amount,) = conn.execute("SELECT investment_amount FROM manual_holdings WHERE id = ?",
                             (row["id"],)).fetchone()
    conn.close()
    assert amount == 900000.0          # value untouched


def test_touch_is_owner_scoped(client):
    uid, _ = _login("owner@test.com")
    _ppf(uid, 400)
    row = storage.stale_entries(uid)[0]
    other = storage.get_or_create_user("intruder@test.com").id
    storage.touch_row(row["source"], row["id"], other)
    assert len(storage.stale_entries(uid)) == 1      # not cleared by someone else


def test_touch_rejects_a_table_that_is_not_a_stale_source(client):
    """`source` arrives from a form, and the table name goes into SQL."""
    uid, _ = _login("inject@test.com")
    storage.touch_row("users; DROP TABLE users", 1, uid)
    storage.touch_row("sessions", 1, uid)
    assert storage.get_or_create_user("inject@test.com").id == uid   # still there


# --- Surfaced ------------------------------------------------------------------

def test_dashboard_shows_the_worklist_with_a_link_to_each_row(client):
    import app.main as m
    uid, ck = _login("dash@test.com")
    _ppf(uid, 400, name="PPF SBI")
    body = client.get("/", cookies=ck).text
    assert "worth a second look" in body
    assert "PPF SBI" in body
    # Links to the row's own edit form, not just the leaf: the value is turning
    # "update everything" into one specific thing.
    row_id = storage.stale_entries(uid)[0]["id"]
    # Path derived from the tree, not typed here: PPF sits under fixed-income,
    # and hardcoding the shape would make this test a second source of truth.
    path = m._leaf_paths()["ppf"]
    assert f"/networth/{path}?edit={row_id}" in body


def test_dashboard_is_quiet_with_nothing_stale(client):
    uid, ck = _login("clean@test.com")
    _ppf(uid, 5)
    assert "worth a second look" not in client.get("/", cookies=ck).text


def test_weekly_digest_names_only_the_oldest(client):
    """A digest that lists nine chores a week is a digest people filter."""
    uid, _ = _login("digest@test.com")
    _ppf(uid, 500, name="PPF SBI")
    _ppf(uid, 400, name="LIC policy")
    _ppf(uid, 300, name="NSC certificate")
    import app.main as m
    dash = m._dashboard(storage.get_or_create_user("digest@test.com"))
    html, _ = digest.weekly_email(digest.ist_today(), dash, {}, None)
    assert "PPF SBI" in html
    assert "LIC policy" not in html and "NSC certificate" not in html
    assert "and 2 others" in html


def test_digest_escapes_the_label(client):
    uid, _ = _login("esc@test.com")
    _ppf(uid, 400, name="<script>x</script>")
    import app.main as m
    dash = m._dashboard(storage.get_or_create_user("esc@test.com"))
    html, _ = digest.weekly_email(digest.ist_today(), dash, {}, None)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
