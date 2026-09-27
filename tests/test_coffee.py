"""The tip jar. Small, but it's the one place the site asks for money."""

import pytest
from fastapi.testclient import TestClient

from app import prices, storage

BMC = "https://buymeacoffee.com/manoj"


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


def test_absent_entirely_when_unconfigured(client, monkeypatch):
    """A self-hosted copy must never solicit money on someone else's behalf, and
    a dead donate link is worse than no link. Same fail-closed shape as
    OWNER_EMAIL and SUPPORT_EMAIL."""
    monkeypatch.delenv("COFFEE_URL", raising=False)
    assert client.get("/buy-me-a-coffee").status_code == 404
    assert "/buy-me-a-coffee" not in client.get("/").text


def test_a_username_becomes_a_buymeacoffee_link(client, monkeypatch):
    monkeypatch.setenv("COFFEE_URL", "manoj")
    body = client.get("/buy-me-a-coffee").text
    assert f'href="{BMC}"' in body
    # The heart is wrapped so it can be red; assert on the pieces, not the
    # rendered string, or any styling change breaks the test.
    assert "Built with" in body and '<span class="heart">♥</span> for India' in body


@pytest.mark.parametrize("raw", ["@manoj", "manoj/", " manoj "])
def test_a_username_pasted_with_decoration_still_works(client, monkeypatch, raw):
    """People copy '@handle' or a trailing slash out of a profile page."""
    monkeypatch.setenv("COFFEE_URL", raw)
    assert f'href="{BMC}"' in client.get("/buy-me-a-coffee").text


@pytest.mark.parametrize("url", ["https://ko-fi.com/manoj", "https://buymeacoffee.com/x"])
def test_a_full_url_is_taken_as_given(client, monkeypatch, url):
    """Moving to Ko-fi or anything else is an env change, not a deploy."""
    monkeypatch.setenv("COFFEE_URL", url)
    assert f'href="{url}"' in client.get("/buy-me-a-coffee").text


def test_no_preset_amounts_are_invented(client, monkeypatch):
    """Buy Me a Coffee picks the quantity on its own page. Guessing a query
    parameter it may not honour would produce links that look precise and
    quietly do nothing."""
    monkeypatch.setenv("COFFEE_URL", "manoj")
    body = client.get("/buy-me-a-coffee").text
    assert "buymeacoffee.com/manoj?" not in body
    assert "buymeacoffee.com/manoj/" not in body


def test_footer_link_appears_once_configured(client, monkeypatch):
    monkeypatch.setenv("COFFEE_URL", "manoj")
    assert "/buy-me-a-coffee" in client.get("/").text          # public landing
    assert "/buy-me-a-coffee" in client.get("/about").text     # and content pages


def test_no_third_party_script_is_loaded(client, monkeypatch):
    """A plain link, not BMC's widget — nothing external runs on the page."""
    monkeypatch.setenv("COFFEE_URL", "manoj")
    body = client.get("/buy-me-a-coffee").text
    assert "buymeacoffee.com/widget" not in body
    assert '<script src="https://' not in body


def test_the_link_does_not_leak_the_referrer(client, monkeypatch):
    """rel=noreferrer, so BMC isn't told which page they came from — that page
    can be a signed-in URL naming which asset classes someone holds."""
    monkeypatch.setenv("COFFEE_URL", "manoj")
    assert 'rel="noopener noreferrer"' in client.get("/buy-me-a-coffee").text


def test_it_is_not_in_the_sitemap(client, monkeypatch):
    """Public so it's reachable, but a tip jar is not content to rank."""
    monkeypatch.setenv("COFFEE_URL", "manoj")
    assert "/buy-me-a-coffee" not in client.get("/sitemap.xml").text
