"""Tests for physical-gold valuation: flat value vs weight × karat × live rate."""

import pytest

from app import main, prices


def test_gold_inr_per_gram_derivation(monkeypatch):
    # GC=F is USD per troy ounce; INR=X is USD→INR.
    monkeypatch.setattr(prices, "get_quote", lambda s: {"GC=F": 3110.34768, "INR=X": 100.0}.get(s))
    # 3110.34768 / 31.1034768 g/oz × 100 = 10000 INR/g (24k)
    assert prices.gold_inr_per_gram() == pytest.approx(10000.0)


def test_price_gold_flat_and_weight(monkeypatch):
    monkeypatch.setattr(prices, "gold_inr_per_gram", lambda: 10000.0)  # 24k INR/g
    rows = [
        {"weight_g": 50.0, "karat": 24, "flat_value": None},   # 50 × 10000 = 500,000
        {"weight_g": 100.0, "karat": 22, "flat_value": None},  # 100 × 10000 × .916 = 916,000
        {"weight_g": None, "karat": None, "flat_value": 200000.0},  # flat
        {"weight_g": None, "karat": None, "flat_value": None},  # unpriced
    ]
    rate = main._price_gold(rows)
    assert rate == 10000.0
    assert rows[0]["value"] == pytest.approx(500000.0) and rows[0]["basis"] == "weight"
    assert rows[1]["value"] == pytest.approx(916000.0)
    assert rows[2]["value"] == 200000.0 and rows[2]["basis"] == "flat"
    assert rows[3]["value"] is None and rows[3]["basis"] is None


def test_price_gold_no_rate_leaves_weight_unpriced(monkeypatch):
    monkeypatch.setattr(prices, "gold_inr_per_gram", lambda: None)
    rows = [
        {"weight_g": 50.0, "karat": 24, "flat_value": None},
        {"weight_g": None, "karat": None, "flat_value": 5000.0},  # flat still works
    ]
    main._price_gold(rows)
    assert rows[0]["value"] is None            # no rate -> can't value by weight
    assert rows[1]["value"] == 5000.0          # flat is independent of the rate
