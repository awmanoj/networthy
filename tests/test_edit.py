"""Tests for the edit flow: the generic `update_row` updater used by every
manual-entry type (Networth leaves + Expenses).

`update_row(table, row_id, user_id, **fields)` must (a) change exactly the given
columns, (b) leave other columns untouched, and (c) refuse to touch a row owned by
a different user — the same isolation guarantee the delete routes rely on.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
    storage.init_db()
    return storage


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


def _login(email="k@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _only_property(db, user_id):
    rows = db.list_property_holdings(user_id, "primary-residence")
    return rows[0]


def test_update_row_changes_given_columns_only(db):
    u = db.get_or_create_user("a@b.com").id
    db.add_property_holding(u, "primary-residence", "Old flat", 1_000_000.0,
                            cost=800_000.0, notes="keep me")
    pid = _only_property(db, u)["id"]

    db.update_row("property_holdings", pid, u,
                  label="New villa", current_value=2_500_000.0)

    row = _only_property(db, u)
    assert row["label"] == "New villa"
    assert row["current_value"] == 2_500_000.0
    assert row["cost"] == 800_000.0        # untouched
    assert row["notes"] == "keep me"       # untouched


def test_update_row_is_scoped_to_owner(db):
    owner = db.get_or_create_user("owner@b.com").id
    other = db.get_or_create_user("other@b.com").id
    db.add_property_holding(owner, "primary-residence", "Owner flat", 1_000_000.0)
    pid = _only_property(db, owner)["id"]

    # A different user must not be able to edit the owner's row.
    db.update_row("property_holdings", pid, other, label="Hijacked")

    assert _only_property(db, owner)["label"] == "Owner flat"   # unchanged


def test_update_row_noop_with_no_fields(db):
    u = db.get_or_create_user("a@b.com").id
    db.add_property_holding(u, "primary-residence", "Flat", 1_000_000.0)
    pid = _only_property(db, u)["id"]
    db.update_row("property_holdings", pid, u)   # no fields -> no-op, no error
    assert _only_property(db, u)["label"] == "Flat"


def test_update_row_edits_an_expense(db):
    u = db.get_or_create_user("a@b.com").id
    db.add_expense(u, "Rent", "housing", 30_000.0, "monthly", count=1)
    eid = db.list_expenses(u)[0]["id"]

    db.update_row("expenses", eid, u, amount=42_000.0, count=2, frequency="monthly")

    row = db.list_expenses(u)[0]
    assert row["amount"] == 42_000.0 and row["count"] == 2
    assert row["name"] == "Rent"           # untouched


# --- End-to-end through the actual routes + templates -----------------------

def test_property_edit_page_prefills_and_saves(client):
    uid, cookies = _login()
    storage.add_property_holding(uid, "primary-residence", "Old flat", 1_000_000.0)
    pid = storage.list_property_holdings(uid, "primary-residence")[0]["id"]

    # The edit view pre-fills the form with the current values + a hidden id.
    r = client.get(f"/networth/assets/non-financial-assets/real-estate/primary-residence?edit={pid}", cookies=cookies)
    assert r.status_code == 200
    assert "Edit property" in r.text
    assert 'value="Old flat"' in r.text
    assert f'name="id" value="{pid}"' in r.text

    # Posting with that id updates the row rather than inserting a second one.
    client.post("/networth/property/add", cookies=cookies, data={
        "leaf_slug": "primary-residence",
        "redirect": "assets/non-financial-assets/real-estate/primary-residence",
        "label": "New villa", "current_value": "2500000", "id": str(pid),
    })
    rows = storage.list_property_holdings(uid, "primary-residence")
    assert len(rows) == 1                                  # updated in place, not duplicated
    assert rows[0]["label"] == "New villa"
    assert rows[0]["current_value"] == 2_500_000.0


def test_expense_edit_updates_in_place(client):
    uid, cookies = _login()
    storage.add_expense(uid, "Rent", "housing", 30_000.0, "monthly", count=1)
    eid = storage.list_expenses(uid)[0]["id"]

    r = client.get(f"/expenses?edit={eid}", cookies=cookies)
    assert r.status_code == 200 and 'name="id" value="%d"' % eid in r.text

    client.post("/expenses/add", cookies=cookies, data={
        "name": "Rent", "category": "housing", "amount": "45000",
        "frequency": "monthly", "count": "1", "id": str(eid),
    })
    rows = storage.list_expenses(uid)
    assert len(rows) == 1 and rows[0]["amount"] == 45_000.0
