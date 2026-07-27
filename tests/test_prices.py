"""Tests for the live-quote helper. No real network — the HTTP fetch is stubbed."""

import time

import pytest

from app import prices


@pytest.fixture(autouse=True)
def clear_cache():
    prices._cache.clear()
    yield
    prices._cache.clear()


@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("E2E.NSE", "E2E.NS"),
        ("reliance.bse", "RELIANCE.BO"),
        ("HDFCBANK.NSE", "HDFCBANK.NS"),
        ("E2E.XYZ", None),   # unknown exchange — don't guess
        ("", None),
        (None, None),
    ],
)
def test_yahoo_symbol_mapping(ticker, expected):
    assert prices.yahoo_symbol(ticker) == expected


def test_get_quote_caches_and_avoids_refetch(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(symbol):
        calls["n"] += 1
        return 542.5

    monkeypatch.setattr(prices, "_fetch", fake_fetch)

    assert prices.get_quote("E2E.NS") == 542.5
    assert prices.get_quote("E2E.NS") == 542.5   # served from cache
    assert calls["n"] == 1                        # fetched only once


def test_get_quote_expired_cache_refetches(monkeypatch):
    monkeypatch.setattr(prices, "_fetch", lambda s: 100.0)
    prices.get_quote("X.NS")
    # Age the cache entry past the TTL.
    price, _ = prices._cache["X.NS"]
    prices._cache["X.NS"] = (price, time.time() - prices._CACHE_TTL - 1)
    calls = {"n": 0}

    def fake(symbol):
        calls["n"] += 1
        return 110.0

    monkeypatch.setattr(prices, "_fetch", fake)
    assert prices.get_quote("X.NS") == 110.0
    assert calls["n"] == 1


def test_failed_fetch_not_cached(monkeypatch):
    monkeypatch.setattr(prices, "_fetch", lambda s: None)
    assert prices.get_quote("BAD.NS") is None
    assert "BAD.NS" not in prices._cache   # failures don't poison the cache


def test_quotes_for_tickers_maps_by_cas_ticker(monkeypatch):
    monkeypatch.setattr(prices, "get_quote", lambda sym: {"E2E.NS": 542.5}.get(sym))
    out = prices.quotes_for_tickers(["E2E.NSE", "UNKNOWN.XYZ", None])
    assert out == {"E2E.NSE": 542.5}
