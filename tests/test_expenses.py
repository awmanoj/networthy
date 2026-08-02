"""Tests for expense annualisation and the FIRE/runway maths."""

import pytest

from app import expenses


@pytest.mark.parametrize(
    "amount,count,freq,expected",
    [
        (1000.0, 1, "monthly", 12000.0),
        (30000.0, 1, "quarterly", 120000.0),
        (25000.0, 1, "half-yearly", 50000.0),
        (100000.0, 1, "annual", 100000.0),
        (100000.0, 2, "annual", 200000.0),   # count scales (2 kids)
        (5000.0, 4, "monthly", 240000.0),      # 4 members × 5k/mo
    ],
)
def test_annual_amount(amount, count, freq, expected):
    assert expenses.annual_amount(amount, count, freq) == pytest.approx(expected)


def test_annual_amount_unknown_frequency_is_zero():
    assert expenses.annual_amount(1000.0, 1, "weekly") == 0.0


def test_category_and_frequency_lookups():
    assert expenses.category_label("housing") == "Housing"
    assert expenses.category_label("bogus") == "bogus"
    assert expenses.category_color("food").startswith("#")
    assert expenses.frequency_label("half-yearly") == "Half-yearly"
    assert {c["slug"] for c in expenses.CATEGORIES} >= {"housing", "food", "other"}


def test_fire_maths_via_module_constant():
    # 25× annual = FIRE target; runway = net worth / annual.
    annual = 1200000.0
    net_worth = 15000000.0
    assert annual * expenses.FIRE_MULTIPLE == 30000000.0
    assert net_worth / annual == pytest.approx(12.5)   # 12.5 years of runway
