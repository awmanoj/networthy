"""Public marketing surface: landing at / when logged out, footer pages public,
dashboard at / when logged in, and app routes still gated."""

from datetime import datetime, timedelta

import pytest

from app import auth, prices, storage
from fastapi.testclient import TestClient


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


def _login(client):
    uid = storage.get_or_create_user("k@test.com").id
    token = "tok"
    storage.create_session(uid, token, datetime.utcnow() + timedelta(hours=1))
    return {auth.SESSION_COOKIE: token}


def test_anonymous_home_is_public_landing(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "location" not in {k.lower() for k in r.headers}  # not redirected to /login
    assert "Get started" in r.text and "Networthy" in r.text


def test_footer_pages_are_public(client):
    for path in ("/about", "/terms", "/privacy"):
        assert client.get(path, follow_redirects=False).status_code == 200


def test_logged_in_home_is_the_dashboard(client):
    r = client.get("/", cookies=_login(client))
    assert r.status_code == 200
    assert "Know exactly what you're worth" not in r.text  # the landing headline


def test_app_routes_still_gated_when_anonymous(client):
    r = client.get("/expenses", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
