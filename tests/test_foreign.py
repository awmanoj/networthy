"""Tests for foreign (US) equity pricing: live USD × FX -> INR, gain% vs cost."""

import pytest

from app import main, prices


def test_price_foreign_value_and_gain(monkeypatch):
    # Stubbing get_quote covers both the ticker price and usd_inr()'s "INR=X".
    monkeypatch.setattr(prices, "get_quote", lambda s: {"AAPL": 340.0, "INR=X": 95.0}.get(s))
    rows = [{"ticker": "AAPL", "units": 10, "cost_usd": 300.0}]

    fx = main._price_foreign(rows)
    assert fx == 95.0
    assert rows[0]["price_usd"] == 340.0
    assert rows[0]["value"] == pytest.approx(10 * 340.0 * 95.0)      # shares × USD × FX
    assert rows[0]["gain_pct"] == pytest.approx((340.0 / 300.0 - 1) * 100)
    assert rows[0]["signal"] == "up"


def test_price_foreign_no_cost_means_no_gain(monkeypatch):
    monkeypatch.setattr(prices, "get_quote", lambda s: {"MSFT": 400.0, "INR=X": 95.0}.get(s))
    rows = [{"ticker": "MSFT", "units": 2, "cost_usd": None}]
    main._price_foreign(rows)
    assert rows[0]["value"] == pytest.approx(2 * 400.0 * 95.0)
    assert rows[0]["gain_pct"] is None and rows[0]["signal"] is None


def test_price_foreign_unpriced_or_no_fx(monkeypatch):
    monkeypatch.setattr(prices, "get_quote", lambda s: None)  # nothing resolves
    rows = [{"ticker": "XYZ", "units": 5, "cost_usd": 100.0}]
    fx = main._price_foreign(rows)
    assert fx is None
    assert rows[0]["value"] is None and rows[0]["gain_pct"] is None


# --- Foreign currency (forex) -----------------------------------------------

@pytest.mark.parametrize(
    "cur,expected", [("USD", "USDINR=X"), ("eur", "EURINR=X"), ("", None), (None, None)]
)
def test_fx_to_inr_symbol(monkeypatch, cur, expected):
    seen = {}
    monkeypatch.setattr(prices, "get_quote", lambda s: seen.setdefault("sym", s) or 95.0)
    prices.fx_to_inr(cur)
    assert seen.get("sym") == expected


def test_fx_to_inr_identity():
    assert prices.fx_to_inr("INR") == 1.0


def test_price_forex_values_amount_times_rate(monkeypatch):
    monkeypatch.setattr(prices, "fx_to_inr", lambda c: {"USD": 95.0, "EUR": 110.0}.get(c))
    rows = [
        {"currency": "USD", "amount": 5000.0},
        {"currency": "EUR", "amount": 1000.0},
        {"currency": "XXX", "amount": 10.0},   # unknown -> no rate
    ]
    main._price_forex(rows)
    assert rows[0]["value"] == pytest.approx(5000 * 95.0)
    assert rows[1]["value"] == pytest.approx(1000 * 110.0)
    assert rows[2]["rate"] is None and rows[2]["value"] is None


# --- Crypto -----------------------------------------------------------------

def test_price_crypto_value_and_gain(monkeypatch):
    monkeypatch.setattr(prices, "crypto_inr", lambda s: {"BTC": 6000000.0, "ETH": 180000.0}.get(s))
    monkeypatch.setattr(prices, "usd_inr", lambda: 95.0)
    rows = [
        {"symbol": "BTC", "quantity": 0.5, "invested_inr": 2000000.0},  # value 3,000,000
        {"symbol": "ETH", "quantity": 2.0, "invested_inr": None},       # value 360,000, no gain
        {"symbol": "XYZ", "quantity": 1.0, "invested_inr": 100.0},      # unpriced
    ]
    main._price_crypto(rows)
    assert rows[0]["value"] == pytest.approx(3000000.0)
    assert rows[0]["gain_pct"] == pytest.approx(50.0) and rows[0]["signal"] == "up"
    assert rows[1]["value"] == pytest.approx(360000.0) and rows[1]["gain_pct"] is None
    assert rows[2]["value"] is None


def test_crypto_inr_symbol(monkeypatch):
    seen = {}

    def fake_quote(s):
        seen["sym"] = s
        return 60000.0

    monkeypatch.setattr(prices, "get_quote", fake_quote)
    monkeypatch.setattr(prices, "usd_inr", lambda: 95.0)
    assert prices.crypto_inr("btc") == pytest.approx(60000.0 * 95.0)
    assert seen["sym"] == "BTC-USD"   # uppercased, -USD suffix
    assert prices.crypto_inr("") is None
