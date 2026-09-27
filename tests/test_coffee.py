"""The tip jar. Small, but it's the one place the site asks for money."""

import pytest
from fastapi.testclient import TestClient

from app import prices, storage


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
    monkeypatch.delenv("PAYPAL_ME", raising=False)
    assert client.get("/buy-me-a-coffee").status_code == 404
    assert "/buy-me-a-coffee" not in client.get("/").text


def test_amounts_and_a_custom_option(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_ME", "manoj")
    body = client.get("/buy-me-a-coffee").text
    for amount in (5, 10, 25):
        assert f"https://paypal.me/manoj/{amount}USD" in body
    assert 'href="https://paypal.me/manoj"' in body        # let them choose
    assert "Built with ♥" in body


def test_currency_is_settable(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_ME", "manoj")
    monkeypatch.setenv("PAYPAL_CURRENCY", "inr")
    assert "https://paypal.me/manoj/5INR" in client.get("/buy-me-a-coffee").text


@pytest.mark.parametrize("raw", ["@manoj", "manoj/", " manoj "])
def test_a_handle_pasted_with_decoration_still_works(client, monkeypatch, raw):
    """People copy '@handle' or a trailing slash out of PayPal without thinking."""
    monkeypatch.setenv("PAYPAL_ME", raw)
    assert "https://paypal.me/manoj/5USD" in client.get("/buy-me-a-coffee").text


def test_footer_link_appears_once_configured(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_ME", "manoj")
    assert "/buy-me-a-coffee" in client.get("/").text          # public landing
    assert "/buy-me-a-coffee" in client.get("/about").text     # and content pages


def test_no_third_party_script_is_loaded(client, monkeypatch):
    """Plain links, not PayPal's button SDK — nothing external runs on the page."""
    monkeypatch.setenv("PAYPAL_ME", "manoj")
    body = client.get("/buy-me-a-coffee").text
    assert "paypalobjects" not in body and "<script src=\"https://" not in body


def test_links_do_not_leak_the_referrer(client, monkeypatch):
    """rel=noreferrer, so PayPal isn't told which page they came from — that page
    can be a signed-in URL naming what someone holds."""
    monkeypatch.setenv("PAYPAL_ME", "manoj")
    body = client.get("/buy-me-a-coffee").text
    assert body.count('rel="noopener noreferrer"') >= 4


def test_it_is_not_in_the_sitemap(client, monkeypatch):
    """Public so it's reachable, but a tip jar is not content to rank."""
    monkeypatch.setenv("PAYPAL_ME", "manoj")
    assert "/buy-me-a-coffee" not in client.get("/sitemap.xml").text
