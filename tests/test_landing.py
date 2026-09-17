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


# --- The free tools are the top of the funnel, so the landing must carry them ---

def test_landing_links_every_public_tool(client):
    """Two of the three used to be reachable from here only via the footer, which
    is both the least-clicked and the least-weighted place a link can sit."""
    body = client.get("/").text
    for path in ("/how-rich-am-i", "/how-much-do-i-need-to-retire",
                 "/how-do-i-get-to-10-crore"):
        assert f'href="{path}"' in body, f"{path} is missing from the landing page"


def test_tools_strip_figures_come_from_the_real_sources(client):
    """The strip quotes two numbers. Hardcoding them would let the landing drift
    from the pages it links to — these must match what those pages compute."""
    from app import expenses, main, wealth
    body = client.get("/").text
    assert main._inr_short(wealth.wealth_for_top_pct(1.0, "india")) in body
    assert main._inr_short(
        expenses.fire_target(100_000 * 12, expenses.DEFAULT_SWR_PCT)) in body


def test_tools_link_is_on_every_marketing_page(client):
    for path in ("/", "/about", "/privacy", "/terms"):
        assert 'href="/#tools"' in client.get(path).text, path
