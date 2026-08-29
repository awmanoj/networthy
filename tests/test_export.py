"""Taking your data out, and erasing it.

The privacy page promises both. These tests care most about the two ways that
promise quietly becomes false: an export that misses a table the user filled in,
and a deletion that leaves something behind after telling them it's gone.
"""

import io
import json
import zipfile
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, exporter, prices, storage
from app.models import Snapshot


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


def _login(email="export@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _populate(uid):
    """A little of everything, so an export that drops a table shows up."""
    storage.add_bank_cash(uid, "bank-accounts", 250_000.0, "HDFC", "Savings", "Main")
    storage.add_property_holding(uid, "primary-residence", "Flat", 9_000_000.0)
    storage.add_gold_item(uid, description="Coins", flat_value=300_000.0)
    storage.add_crypto_holding(uid, symbol="BTC", quantity=0.1)
    storage.add_foreign_holding(uid, ticker="AAPL", units=5.0)
    storage.add_liability(uid, "home-loan", lender="HDFC", outstanding=1_000_000.0)
    storage.add_expense(uid, "Rent", "housing", 50_000.0, "monthly")
    storage.add_goal(uid, "College", "education", 5_000_000.0)
    storage.save_swr_pct(uid, 2.5)
    storage.save_user_settings(uid, "ABCDE1234F", "")
    storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=1_000_000.0,
        holding_count=1, source_filename="cas.pdf",
    ))


# --- Coverage: the failure mode that matters --------------------------------

def test_export_covers_every_table_that_belongs_to_a_user(tmp_path, monkeypatch):
    """A new per-user table must be added to EXPORT_TABLES deliberately. If this
    fails, someone added a table and the export silently stopped being complete."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()

    with storage._connect() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        with_user_id = {
            t for t in tables
            if any(c["name"] == "user_id" for c in conn.execute(f"PRAGMA table_info({t})"))
        }

    missing = with_user_id - set(exporter.EXPORT_TABLES) - set(exporter._EXCLUDED)
    assert not missing, f"per-user tables missing from the export: {sorted(missing)}"


def test_settings_are_not_forgotten(client):
    """user_settings holds the PAN, withdrawal rate and plan inputs. It isn't in
    demo._TABLES, which is the list you'd reach for first — and omitting it is
    the sort of gap nobody notices until they've moved machines."""
    assert "user_settings" in exporter.EXPORT_TABLES

    uid, _ck = _login()
    _populate(uid)
    data = exporter.collect(uid)
    assert data["user_settings"], "settings row missing from the export"
    assert data["user_settings"][0]["swr_pct"] == 2.5


def test_credentials_are_never_exported(client):
    """A downloaded file holding a live session token would be a security hole,
    not a feature."""
    uid, _ck = _login()
    _populate(uid)
    payload = json.loads(exporter.as_json(uid, "export@test.com"))

    assert "sessions" not in payload["data"]
    assert "login_codes" not in payload["data"]
    # The session token itself must not appear as a value anywhere.
    assert '"tok"' not in json.dumps(payload["data"])


# --- The formats ------------------------------------------------------------

def test_json_export_is_complete_and_parseable(client):
    uid, ck = _login()
    _populate(uid)

    r = client.get("/account/export.json", cookies=ck)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "attachment" in r.headers["content-disposition"]

    payload = json.loads(r.text)
    assert payload["networthy_export"]["account"] == "export@test.com"
    data = payload["data"]
    assert data["expenses"][0]["name"] == "Rent"
    assert data["bank_cash"][0]["balance"] == 250_000.0
    assert data["snapshots"][0]["statement_date"] == "2026-06-30"


def test_csv_zip_has_one_file_per_populated_table(client):
    uid, ck = _login()
    _populate(uid)

    r = client.get("/account/export.zip", cookies=ck)
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "networthy/expenses.csv" in names
    assert "networthy/README.txt" in names
    # Empty tables are skipped rather than shipped as a lone header row.
    assert "networthy/business_holdings.csv" not in names

    rows = zf.read("networthy/expenses.csv").decode().splitlines()
    assert rows[0].startswith("id,user_id,name") or "name" in rows[0]
    assert "Rent" in rows[1]


def test_export_is_scoped_to_the_signed_in_user(client):
    mine, ck = _login("mine@test.com")
    _populate(mine)
    theirs = storage.get_or_create_user("theirs@test.com").id
    storage.add_expense(theirs, "Not yours", "other", 1.0, "monthly")

    body = client.get("/account/export.json", cookies=ck).text
    assert "Rent" in body and "Not yours" not in body


def test_export_requires_a_session(client):
    for path in ("/account", "/account/export.json", "/account/export.zip"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


# --- Deletion ---------------------------------------------------------------

def test_delete_removes_everything_the_export_would_have_returned(client):
    """Export and delete must cover the same ground, or a user is told their data
    is gone while some of it quietly survives."""
    uid, ck = _login("bye@test.com")
    _populate(uid)
    assert any(exporter.collect(uid).values())

    r = client.post("/account/delete", data={"confirm": "DELETE"},
                    cookies=ck, follow_redirects=False)
    assert r.status_code == 303

    left = {t: rows for t, rows in exporter.collect(uid).items() if rows}
    assert not left, f"survived deletion: {list(left)}"


def test_delete_logs_you_out(client):
    uid, ck = _login("bye2@test.com")
    _populate(uid)
    client.post("/account/delete", data={"confirm": "DELETE"}, cookies=ck,
                follow_redirects=False)
    # The old session must not still work.
    r = client.get("/networth", cookies=ck, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_delete_needs_the_confirmation_word(client):
    uid, ck = _login("keep@test.com")
    _populate(uid)

    for wrong in ("", "delete me", "yes", "DELET"):
        r = client.post("/account/delete", data={"confirm": wrong}, cookies=ck,
                        follow_redirects=False)
        assert r.status_code == 303 and "confirm=1" in r.headers["location"]
    assert exporter.collect(uid)["expenses"], "data was deleted without confirmation"

    # Lowercase and stray whitespace are accepted — the guard is against a
    # mis-click, not a typing test.
    r = client.post("/account/delete", data={"confirm": " delete "}, cookies=ck,
                    follow_redirects=False)
    assert not exporter.collect(uid)["expenses"]


def test_the_shared_demo_cannot_be_deleted(client):
    """One visitor emptying the demo would break it for everyone."""
    from app import demo
    r = client.get("/demo", follow_redirects=False)
    ck = {auth.SESSION_COOKIE: r.cookies[auth.SESSION_COOKIE]}
    uid = storage.get_or_create_user(demo.DEMO_EMAIL).id

    client.post("/account/delete", data={"confirm": "DELETE"}, cookies=ck,
                follow_redirects=False)
    assert storage.list_expenses(uid), "the demo account was wiped by a visitor"


def test_delete_leaves_other_accounts_alone(client):
    mine, ck = _login("gone@test.com")
    _populate(mine)
    theirs = storage.get_or_create_user("stays@test.com").id
    _populate(theirs)

    client.post("/account/delete", data={"confirm": "DELETE"}, cookies=ck,
                follow_redirects=False)
    assert storage.list_expenses(theirs), "deleting one account touched another"
