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
