"""What return would it take to reach a target net worth?

The maths is a bisection, so the tests care most about the two ways it could be
plausibly wrong: a rate that doesn't actually land on the target, and a target
that's unreachable being quoted a number anyway.
"""

import pytest

from app import pathto
from app.pathto import PathInputs


def _p(**over):
    base = dict(current=10_000_000.0, target=100_000_000.0, years=20,
                annual_savings=1_200_000.0)
    base.update(over)
    return PathInputs(**base)


# --- The solver -------------------------------------------------------------

def test_the_required_return_actually_lands_on_the_target():
    """The round-trip that matters: compound at the rate it gives back and you
    should arrive at the target, not near it."""
    p = _p()
    r = pathto.required_return(p)
    assert r is not None
    reached = pathto.future_value(p.current, p.annual_savings, p.years, r)
    assert reached == pytest.approx(p.target, rel=1e-4)


def test_a_lower_rate_falls_short_and_a_higher_one_overshoots():
    p = _p()
    r = pathto.required_return(p)
    assert pathto.future_value(p.current, p.annual_savings, p.years, r - 0.5) < p.target
    assert pathto.future_value(p.current, p.annual_savings, p.years, r + 0.5) > p.target


def test_more_time_needs_less_return():
    rates = [pathto.required_return(_p(years=y)) for y in (10, 15, 20, 25, 30)]
    assert rates == sorted(rates, reverse=True)


def test_more_saving_needs_less_return():
    rates = [pathto.required_return(_p(annual_savings=s))
             for s in (0.0, 600_000.0, 1_200_000.0, 2_400_000.0)]
    assert rates == sorted(rates, reverse=True)


def test_already_there_needs_nothing():
    assert pathto.required_return(_p(current=200_000_000.0)) == 0.0


def test_savings_alone_reaching_the_target_needs_nothing():
    # ₹1 cr saved a year for 20 years clears ₹10 cr with no growth at all.
    assert pathto.required_return(
        _p(current=0.0, annual_savings=10_000_000.0)) == 0.0


def test_an_impossible_target_returns_none_rather_than_a_number():
    """A calculator that quotes 340% a year without saying it's impossible is
    worse than one that refuses."""
    assert pathto.required_return(
        PathInputs(current=500_000.0, target=1_000_000_000.0, years=3)) is None


@pytest.mark.parametrize("years,target", [(0, 1e8), (-5, 1e8), (10, 0.0)])
def test_degenerate_inputs_return_none(years, target):
    assert pathto.required_return(
        PathInputs(current=1e7, target=target, years=years)) is None


# --- Reading the answer -----------------------------------------------------

@pytest.mark.parametrize("rate,expected", [
    (4.0, "comfortable"), (8.0, "reasonable"), (11.0, "demanding"),
    (14.0, "a stretch"), (22.0, "not a plan"), (500.0, "not a plan"),
])
def test_verdict_bands(rate, expected):
    assert pathto.verdict(rate)[0] == expected


# --- The portfolio half -----------------------------------------------------

def test_blended_return_weights_by_value_not_count():
    """A ₹9 cr savings account and ₹1 lakh of equity is not a 50/50 portfolio."""
    mix = [{"label": "Bank Account & Cash", "value": 90_000_000.0},
           {"label": "Equity", "value": 100_000.0}]
    blended = pathto.blended_return(mix)
    assert blended == pytest.approx(4.009, abs=0.01)


def test_blended_return_of_an_all_equity_portfolio():
    assert pathto.blended_return(
        [{"label": "Equity", "value": 1.0}]) == pytest.approx(12.0)


def test_unknown_asset_classes_fall_back_rather_than_crash():
    b = pathto.blended_return([{"label": "Something New", "value": 1.0}])
    assert b == pytest.approx(pathto.DEFAULT_ASSET_RETURN)


def test_blended_return_of_nothing_is_none():
    assert pathto.blended_return([]) is None
    assert pathto.blended_return([{"label": "Equity", "value": 0.0}]) is None


# --- The levers -------------------------------------------------------------

def test_savings_needed_lands_on_the_target():
    p = _p(annual_savings=0.0)
    s = pathto.savings_needed(p, 12.0)
    assert pathto.future_value(p.current, s, p.years, 12.0) == pytest.approx(
        p.target, rel=1e-6)


def test_savings_needed_is_zero_when_growth_alone_suffices():
    assert pathto.savings_needed(_p(current=90_000_000.0), 12.0) == 0.0


def test_years_to_target_is_consistent_with_the_required_return():
    """Solve for the rate at 20 years, then ask how long that rate takes — it
    should be the same 20 years, or the two halves disagree on screen."""
    p = _p()
    r = pathto.required_return(p)
    assert pathto.years_to_target(p, r) == p.years


def test_years_to_target_gives_up_rather_than_looping_forever():
    p = PathInputs(current=100_000.0, target=1_000_000_000.0, years=10)
    assert pathto.years_to_target(p, 1.0, cap=60) is None
