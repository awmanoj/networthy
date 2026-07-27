"""Tests for the live-quote helper. No real network — the HTTP fetch is stubbed."""

import time

import pytest

from app import prices


@pytest.fixture(autouse=True)
def clear_cache():
    prices._cache.clear()
    prices._amfi_cache = None
    yield
    prices._cache.clear()
    prices._amfi_cache = None


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


# --- AMFI NAV feed ----------------------------------------------------------

_AMFI_SAMPLE = """\
Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Nippon India Mutual Fund

100377;INF204K01562;INF204K01570;Nippon India Large Cap Fund - Growth;88.3163;24-Jul-2026
100378;INF03VN01563;-;White Oak Midcap - Growth;21.360;24-Jul-2026
109999;INF000000000;-;Some Scheme With No NAV;N.A.;24-Jul-2026
"""


def test_parse_amfi_maps_both_isin_columns_and_skips_na():
    table = prices._parse_amfi(_AMFI_SAMPLE)
    assert table["INF204K01562"] == pytest.approx(88.3163)
    assert table["INF204K01570"] == pytest.approx(88.3163)  # reinvest ISIN -> same NAV
    assert table["INF03VN01563"] == pytest.approx(21.360)
    assert "INF000000000" not in table                      # "N.A." row skipped
    assert "Nippon India Mutual Fund" not in table          # header line ignored


def test_navs_for_isins_looks_up_locally(monkeypatch):
    monkeypatch.setattr(prices, "_fetch_amfi", lambda: prices._parse_amfi(_AMFI_SAMPLE))
    out = prices.navs_for_isins(["INF03VN01563", "INEUNKNOWN01", None])
    assert out == {"INF03VN01563": pytest.approx(21.360)}


def test_amfi_map_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return {"INF204K01562": 88.32}

    monkeypatch.setattr(prices, "_fetch_amfi", fake_fetch)
    prices.navs_for_isins(["INF204K01562"])
    prices.navs_for_isins(["INF204K01562"])
    assert calls["n"] == 1  # fetched once, served from the cached map thereafter


def test_amfi_fetch_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(prices, "_fetch_amfi", lambda: {})
    assert prices.navs_for_isins(["INF204K01562"]) == {}
