"""Tests for business analytics: metrics computation, demo exclusion, and the
owner-only gate on /admin."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import analytics, auth, demo, prices, storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
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
    monkeypatch.setattr(auth, "cookie_secure", lambda: False)
    import app.main as m
    return TestClient(m.app)


def _session(uid, token="tok"):
    storage.create_session(uid, token, datetime.utcnow() + timedelta(hours=1))
    return {auth.SESSION_COOKIE: token}


# --- Metrics ----------------------------------------------------------------

def test_overview_counts_exclude_demo(db):
    demo_id = db.get_or_create_user(demo.DEMO_EMAIL).id
    db.record_login(demo_id)                       # demo activity must not count
    a = db.get_or_create_user("a@x.com").id
    b = db.get_or_create_user("b@x.com").id
    db.record_login(a); db.record_login(a); db.record_login(b)
    db.add_goal(a, "G", "wealth", 1_000_000.0)
    db.add_expense(b, "Rent", "housing", 30_000.0, "monthly")

    o = analytics.overview()
    assert o["total_users"] == 2                   # demo excluded
    assert o["logins_total"] == 3                  # demo login excluded
    assert demo.DEMO_EMAIL not in [r["email"] for r in o["recent"]]

    feat = {f["label"]: f["users"] for f in o["features"]}
    assert feat["Set a goal"] == 1 and feat["Tracked expenses"] == 1
    # Both users put in real data (a goal, an expense) -> activated = 2.
    funnel = {f["label"]: f["users"] for f in o["funnel"]}
    assert funnel["Signed up"] == 2 and funnel["Added real data"] == 2


def test_returning_needs_two_distinct_days(db):
    a = db.get_or_create_user("a@x.com").id
    db.record_login(a); db.record_login(a)         # two logins, same day
    assert analytics.overview()["returning"] == 0
    # Backdate one login to yesterday -> now spans two days.
    with storage._connect() as conn:
        y = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE login_events SET created_at=? WHERE id=(SELECT MIN(id) FROM login_events)",
            (y,))
    assert analytics.overview()["returning"] == 1


def test_record_login_fires_on_session_start(db):
    uid = db.get_or_create_user("a@x.com").id
    auth.start_session(uid)
    assert analytics.overview()["logins_total"] == 1


# --- Owner gate -------------------------------------------------------------

def test_admin_requires_owner(client, monkeypatch):
    monkeypatch.setattr(auth, "owner_email", lambda: "owner@hq.com")

    # anonymous -> redirected to login by the gate middleware
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"

    # logged-in non-owner -> 404 (existence hidden)
    other = storage.get_or_create_user("someone@else.com").id
    assert client.get("/admin", cookies=_session(other, "t2")).status_code == 404

    # owner -> 200 with the dashboard
    owner = storage.get_or_create_user("owner@hq.com").id
    storage.get_or_create_user("visible@user.com")
    r = client.get("/admin", cookies=_session(owner, "t3"))
    assert r.status_code == 200 and "Analytics" in r.text
    assert "visible@user.com" in r.text            # the email listing


def test_admin_is_closed_when_no_owner_is_configured(client, monkeypatch):
    """Unconfigured must mean closed. With OWNER_EMAIL unset the owner is the
    empty string, and /admin has to 404 for everyone rather than match anyone."""
    monkeypatch.setattr(auth, "owner_email", lambda: "")
    uid = storage.get_or_create_user("anyone@example.com").id
    assert client.get("/admin", cookies=_session(uid, "t9")).status_code == 404


def test_owner_email_is_unset_by_default(monkeypatch):
    """No hardcoded address: a real one in a public repo would advertise which
    inbox to target to reach /admin."""
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    assert auth.owner_email() == ""

    monkeypatch.setenv("OWNER_EMAIL", "  Owner@Example.COM ")
    assert auth.owner_email() == "owner@example.com"      # trimmed, lowercased
