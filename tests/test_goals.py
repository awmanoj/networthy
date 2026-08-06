"""Tests for financial goals: the SIP planning math and the add/edit/delete routes
plus the read-only FIRE mirror."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, goals, prices, storage


# --- Planning math ----------------------------------------------------------

def test_plan_active_required_monthly():
    # ₹8L saved toward ₹50L, ~8.75 yrs out at 11% -> a positive SIP, "active".
    p = goals.plan(5_000_000, 800_000, date(2035, 6, 1), 11.0, today=date(2026, 8, 5))
    assert p["status"] == "active"
    assert p["months_left"] == 105
    assert p["progress_pct"] == pytest.approx(16.0)
    assert 15_000 < p["required_monthly"] < 20_000     # ~₹17.6k


def test_plan_required_monthly_matches_annuity_formula():
    target, saved, r = 1_200_000, 0, 0.0
    p = goals.plan(target, saved, date(2027, 8, 5), r, today=date(2026, 8, 5))
    assert p["required_monthly"] == pytest.approx(100_000.0)   # 1.2L / 12 months, no return


def test_plan_lumpsum_compounds_to_close_the_gap():
    # A one-time lump sum today, grown at the rate, should equal the funding gap.
    p = goals.plan(5_000_000, 800_000, date(2035, 6, 1), 11.0, today=date(2026, 8, 5))
    gap = p["projected"] and (5_000_000 - p["projected"])
    monthly_r = 1.11 ** (1 / 12) - 1
    grown = p["required_lumpsum"] * (1 + monthly_r) ** p["months_left"]
    assert grown == pytest.approx(gap, rel=1e-6)
    # And a single lump sum is a smaller total outlay than summing every SIP.
    assert p["required_lumpsum"] < p["required_monthly"] * p["months_left"]


def test_plan_funded_when_projection_reaches_target():
    p = goals.plan(1_000_000, 950_000, date(2028, 8, 5), 12.0, today=date(2026, 8, 5))
    assert p["status"] == "funded"
    assert p["required_monthly"] == 0.0 and p["required_lumpsum"] == 0.0


def test_plan_undated_goal_has_no_sip():
    p = goals.plan(1_000_000, 100_000, None, 10.0, today=date(2026, 8, 5))
    assert p["status"] == "undated" and p["required_monthly"] is None
    assert p["required_lumpsum"] is None and p["months_left"] is None


def test_plan_overdue_when_date_passed_and_short():
    p = goals.plan(1_000_000, 100_000, date(2025, 1, 1), 10.0, today=date(2026, 8, 5))
    assert p["status"] == "overdue"
    assert p["required_monthly"] is None and p["required_lumpsum"] is None


def test_months_between():
    assert goals._months_between(date(2026, 8, 5), date(2027, 8, 5)) == 12
    assert goals._months_between(date(2026, 8, 20), date(2027, 8, 5)) == 11  # day-of-month
    assert goals._months_between(date(2026, 8, 5), date(2026, 8, 5)) == 0
    assert goals._months_between(date(2026, 8, 5), date(2020, 1, 1)) == 0    # past clamps to 0


# --- Routes -----------------------------------------------------------------

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


def test_goal_add_list_and_render(client):
    uid, ck = _login()
    client.post("/goals/add", cookies=ck, data={
        "name": "Kids education", "category": "education",
        "target_amount": "5000000", "saved_amount": "800000",
        "target_date": "2035-06-01", "return_pct": "11",
    })
    rows = storage.list_goals(uid)
    assert len(rows) == 1 and rows[0]["name"] == "Kids education"
    page = client.get("/goals", cookies=ck).text
    assert "Kids education" in page and "Invest" in page


def test_goal_edit_updates_in_place(client):
    uid, ck = _login()
    storage.add_goal(uid, "Car", "vehicle", 1_000_000.0, saved_amount=100_000.0)
    gid = storage.list_goals(uid)[0]["id"]

    ep = client.get(f"/goals?edit={gid}", cookies=ck).text
    assert "Edit goal" in ep and f'name="id" value="{gid}"' in ep

    client.post("/goals/add", cookies=ck, data={
        "name": "Car", "category": "vehicle", "target_amount": "1500000",
        "saved_amount": "250000", "id": str(gid),
    })
    rows = storage.list_goals(uid)
    assert len(rows) == 1                              # updated, not duplicated
    assert rows[0]["target_amount"] == 1_500_000.0 and rows[0]["saved_amount"] == 250_000.0


def test_goal_delete(client):
    uid, ck = _login()
    storage.add_goal(uid, "Trip", "travel", 300_000.0)
    gid = storage.list_goals(uid)[0]["id"]
    client.post(f"/goals/{gid}/delete", cookies=ck)
    assert storage.list_goals(uid) == []


def test_fire_mirror_shows_only_with_expenses(client):
    uid, ck = _login()
    # "safe-withdrawal corpus" is unique to the FIRE card (not the page intro).
    assert "safe-withdrawal corpus" not in client.get("/goals", cookies=ck).text
    # Add an expense -> FIRE mirror appears.
    storage.add_expense(uid, "Rent", "housing", 50_000.0, "monthly", count=1)
    assert "safe-withdrawal corpus" in client.get("/goals", cookies=ck).text
