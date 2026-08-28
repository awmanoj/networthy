"""Tests for the one-click public demo account."""

import pytest
from fastapi.testclient import TestClient

from app import auth, demo, prices, storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    monkeypatch.setattr(auth, "cookie_secure", lambda: False)  # let the cookie flow over http
    import app.main as m
    return TestClient(m.app)


def test_reset_seeds_a_populated_portfolio(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    uid = storage.get_or_create_user(demo.DEMO_EMAIL).id
    demo.reset(uid)
    assert len(storage.list_goals(uid)) == 4
    assert len(storage.list_expenses(uid)) == 9
    assert len(storage.list_snapshots(uid)) == 4
    assert len(storage.list_property_holdings(uid, "primary-residence")) == 1
    assert len(storage.list_crypto_holdings(uid)) == 2
    assert len(storage.list_foreign_holdings(uid)) == 2


def test_reset_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    uid = storage.get_or_create_user(demo.DEMO_EMAIL).id
    demo.reset(uid)
    demo.reset(uid)  # a second entry must not duplicate rows
    assert len(storage.list_goals(uid)) == 4
    assert len(storage.list_expenses(uid)) == 9


def test_demo_route_logs_in_and_seeds(client):
    r = client.get("/demo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "session" in r.cookies                       # a session was opened

    client.cookies.update(r.cookies)
    dash = client.get("/")                               # now the Dashboard, not the landing
    assert 'user-email">demo@networthyhq.com' in dash.text
    assert "demo-banner" in dash.text                    # the "you're in the demo" banner
    assert client.get("/goals").status_code == 200


def test_create_your_own_leaves_demo_then_reaches_login(client):
    r = client.get("/demo", follow_redirects=False)
    client.cookies.update(r.cookies)
    # The banner's "Create your own" posts to /logout (a bare /login link would bounce
    # a logged-in user back to /). Logout clears the session and lands on /login.
    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    client.cookies.clear()
    client.cookies.update(out.cookies)
    assert client.get("/login", follow_redirects=False).status_code == 200  # form, not a bounce


def test_demo_route_is_public():
    # /demo must be reachable without a session.
    assert auth._is_public("/demo")


def test_landing_shows_demo_cta(client):
    page = client.get("/").text                          # anonymous -> landing
    assert "/demo" in page and "Explore the live demo" in page


def test_is_demo_helper():
    assert demo.is_demo(storage.User(email=demo.DEMO_EMAIL, id=1))
    assert not demo.is_demo(storage.User(email="someone@else.com", id=2))
    assert not demo.is_demo(None)


# --- Reset throttling -------------------------------------------------------
#
# The demo account is shared. Resetting on every entry was fine for a trickle of
# visitors and broken for a crowd: arrivals would wipe and re-seed it under each
# other, mid-browse, precisely when the most people were watching.

def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    return storage.get_or_create_user(demo.DEMO_EMAIL).id


def test_first_visit_seeds_then_further_visits_leave_it_alone(tmp_path, monkeypatch):
    uid = _fresh(tmp_path, monkeypatch)
    assert demo.reset_if_stale(uid) is True          # nothing there yet -> seed
    assert demo.reset_if_stale(uid) is False         # within the cooldown
    assert demo.reset_if_stale(uid) is False


def test_a_visitors_edits_survive_the_next_visitor(tmp_path, monkeypatch):
    """The actual symptom: someone browsing shouldn't have the account yanked
    out from under them because somebody else opened /demo."""
    uid = _fresh(tmp_path, monkeypatch)
    demo.reset_if_stale(uid)
    storage.add_expense(uid, "Visitor's row", "other", 123.0, "monthly")

    demo.reset_if_stale(uid)                          # a second visitor arrives
    names = [e["name"] for e in storage.list_expenses(uid)]
    assert "Visitor's row" in names


def test_the_cooldown_expires(tmp_path, monkeypatch):
    uid = _fresh(tmp_path, monkeypatch)
    demo.reset_if_stale(uid)
    storage.add_expense(uid, "Stale marker", "other", 1.0, "monthly")

    # Age the throttle past the interval rather than sleeping for ten minutes.
    with storage._connect() as conn:
        conn.execute("UPDATE app_state SET updated_at = datetime('now', '-1 hour') "
                     "WHERE key = 'demo_reset'")
    assert demo.reset_if_stale(uid) is True
    assert "Stale marker" not in [e["name"] for e in storage.list_expenses(uid)]


def test_an_emptied_demo_reseeds_immediately(tmp_path, monkeypatch):
    """A visitor can delete rows. Without this, one person clearing the account
    would serve everyone a blank demo for the rest of the cooldown."""
    uid = _fresh(tmp_path, monkeypatch)
    demo.reset_if_stale(uid)
    storage.clear_user_tables(uid, demo._TABLES)      # someone wipes it

    assert demo.reset_if_stale(uid) is True           # ignores the throttle
    assert storage.list_expenses(uid)


def test_only_one_of_many_simultaneous_visitors_reseeds(tmp_path, monkeypatch):
    """Read-then-write would let every caller in a burst decide to reset. The
    claim is a conditional upsert, so exactly one wins."""
    _fresh(tmp_path, monkeypatch)
    claims = [storage.claim_throttled_action("burst", 600) for _ in range(25)]
    assert claims.count(True) == 1
