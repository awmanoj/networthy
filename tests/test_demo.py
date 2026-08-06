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
