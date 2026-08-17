"""Tests for the lifetime cash-flow projection.

These pin the two modelling decisions that carry the whole feature — savings
before retirement / expenses after it, and goals as pure nominal outflows — plus
the arithmetic itself, so tuning the presentation later can't silently move the
numbers.
"""

from datetime import date

import pytest

from app import projection
from app.projection import PlanInputs


TODAY = date(2026, 1, 1)


def _inputs(**over) -> PlanInputs:
    base = dict(
        current_age=40, retire_age=60, annual_savings=1_000_000.0,
        corpus=10_000_000.0, annual_expense=1_200_000.0,
        return_pct=10.0, inflation_pct=6.0,
    )
    base.update(over)
    return PlanInputs(**base)


# --- The year loop ----------------------------------------------------------

def test_first_year_arithmetic_is_exactly_as_documented():
    """opening + growth + savings - expenses - outflows, flows at year end."""
    rows = projection.project(_inputs(), TODAY)
    y0 = rows[0]
    assert y0.age == 40 and y0.year == 2026
    assert y0.opening == 10_000_000.0
    assert y0.growth == pytest.approx(1_000_000.0)        # 10% of opening
    assert y0.savings == pytest.approx(1_000_000.0)       # year 0, no inflation yet
    assert y0.expenses == 0.0                             # still working
    assert y0.closing == pytest.approx(12_000_000.0)


def test_runs_from_current_age_to_end_age_inclusive():
    rows = projection.project(_inputs(current_age=40, end_age=95), TODAY)
    assert rows[0].age == 40 and rows[-1].age == 95
    assert len(rows) == 56


def test_savings_and_expenses_both_track_inflation():
    rows = projection.project(_inputs(), TODAY)
    # Year 5 savings = 10L * 1.06^5; year at 60 draws the burn inflated 20 years.
    assert rows[5].savings == pytest.approx(1_000_000.0 * 1.06 ** 5)
    at_60 = next(r for r in rows if r.age == 60)
    assert at_60.expenses == pytest.approx(1_200_000.0 * 1.06 ** 20)


# --- The decision that avoids double-counting -------------------------------

def test_expenses_are_not_charged_while_working():
    """annual_savings is already net of living costs — charging the burn too
    during the accumulation years would count it twice."""
    rows = projection.project(_inputs(), TODAY)
    assert all(r.expenses == 0.0 for r in rows if r.age < 60)
    assert all(r.expenses > 0.0 for r in rows if r.age >= 60)


def test_savings_stop_at_retirement():
    rows = projection.project(_inputs(), TODAY)
    assert all(r.savings > 0.0 for r in rows if r.age < 60)
    assert all(r.savings == 0.0 for r in rows if r.age >= 60)
    assert all(r.retired for r in rows if r.age >= 60)


def test_already_retired_draws_from_year_one():
    rows = projection.project(_inputs(current_age=65, retire_age=60), TODAY)
    assert rows[0].retired and rows[0].savings == 0.0
    assert rows[0].expenses == pytest.approx(1_200_000.0)


# --- Goals as one-off outflows ----------------------------------------------

def test_goal_is_taken_in_its_year_at_its_nominal_amount():
    p = _inputs(outflows=((5, 5_000_000.0, "College"),))
    rows = projection.project(p, TODAY)
    hit = next(r for r in rows if r.outflows)
    assert hit.age == 45 and hit.year == 2031
    assert hit.outflows == pytest.approx(5_000_000.0)     # not inflated again
    assert hit.outflow_labels == ("College",)
    assert sum(r.outflows for r in rows) == pytest.approx(5_000_000.0)


def test_several_goals_in_one_year_are_summed():
    p = _inputs(outflows=((3, 2_000_000.0, "Marriage"), (3, 1_000_000.0, "Car")))
    rows = projection.project(p, TODAY)
    hit = next(r for r in rows if r.outflows)
    assert hit.outflows == pytest.approx(3_000_000.0)
    assert set(hit.outflow_labels) == {"Marriage", "Car"}


def test_goals_reduce_the_corpus_relative_to_no_goals():
    without = projection.project(_inputs(), TODAY)[-1].closing
    with_goal = projection.project(
        _inputs(outflows=((5, 5_000_000.0, "College"),)), TODAY)[-1].closing
    assert with_goal < without


# --- Depletion --------------------------------------------------------------

def test_corpus_that_runs_out_reports_the_age_and_clamps_at_zero():
    # Tiny corpus, no savings, already retired, big burn.
    p = _inputs(current_age=60, retire_age=60, corpus=2_000_000.0,
                annual_savings=0.0, annual_expense=2_000_000.0)
    rows = projection.project(p, TODAY)
    gone = projection.depletion_age(rows)
    assert gone is not None and 60 <= gone <= 62
    assert all(r.closing >= 0.0 for r in rows)            # never renders negative
    assert rows[-1].closing == 0.0


def test_balance_is_also_reported_in_todays_money():
    """A nominal balance 50 years out is mostly inflation. The real series is what
    the UI shows, so it has to be right: year 0 is unchanged, and later years
    deflate by exactly the inflation assumption."""
    rows = projection.project(_inputs(), TODAY)
    assert rows[0].real_closing == pytest.approx(rows[0].closing)
    assert rows[20].real_closing == pytest.approx(rows[20].closing / 1.06 ** 20)
    # And the real curve stays far below the nominal one it's derived from.
    assert rows[-1].real_closing < rows[-1].closing / 10


def test_summary_reports_both_nominal_and_real():
    p = _inputs()
    s = projection.summarise(projection.project(p, TODAY), p)
    assert s["final_corpus_real"] < s["final_corpus"]
    assert s["corpus_at_retirement_real"] == pytest.approx(
        s["corpus_at_retirement"] / 1.06 ** 20)


def test_healthy_plan_never_depletes():
    rows = projection.project(_inputs(), TODAY)
    assert projection.depletion_age(rows) is None
    assert projection.summarise(rows, _inputs())["lasts"] is True


def test_summary_reports_retirement_corpus_and_totals():
    p = _inputs(outflows=((5, 5_000_000.0, "College"),))
    s = projection.summarise(projection.project(p, TODAY), p)
    assert s["total_outflows"] == pytest.approx(5_000_000.0)
    assert s["corpus_at_retirement"] > 0
    assert s["years_projected"] == 56 and s["end_age"] == 95


# --- The band ---------------------------------------------------------------

def test_band_brackets_the_base_case():
    """The whole point of three lines: the base must sit inside the range, so
    the output reads as a range rather than a forecast."""
    band = projection.project_band(_inputs(), TODAY)
    lo, base, hi = band["low"][-1].closing, band["base"][-1].closing, band["high"][-1].closing
    assert lo < base < hi
    assert band["delta_pct"] == projection.BAND_DELTA_PCT


def test_band_low_line_uses_a_lower_return_and_cannot_go_negative():
    band = projection.project_band(_inputs(return_pct=1.0), TODAY, delta_pct=2.0)
    # 1% - 2% would be a negative return; it clamps at zero rather than shrinking.
    assert band["low"][0].growth == 0.0


def test_band_summaries_can_disagree_about_lasting():
    """A plan that survives at 10% but not at 8% is exactly what the band is for."""
    # ₹4 cr drawing ₹24 L/yr: runs dry at 81 if returns come in at 8%, lasts
    # past 95 at 12%. Same plan, opposite conclusions — which is the point.
    p = _inputs(current_age=60, retire_age=60, corpus=40_000_000.0,
                annual_savings=0.0, annual_expense=2_400_000.0)
    band = projection.project_band(p, TODAY)
    assert band["low_summary"]["depletion_age"] == 81
    assert band["summary"]["depletion_age"] == 89
    assert band["high_summary"]["depletion_age"] is None


# --- "How much would I need today?" -----------------------------------------

def _short_plan(**over):
    """A plan that doesn't reach 95: retired, no savings, drawing hard."""
    base = dict(current_age=60, retire_age=60, corpus=20_000_000.0,
                annual_savings=0.0, annual_expense=2_400_000.0)
    base.update(over)
    return _inputs(**base)


def test_gap_is_the_amount_that_actually_fixes_the_plan():
    """The property that matters: add exactly the gap and the plan reaches 95;
    add a little less and it still doesn't."""
    p = _short_plan()
    need = projection.corpus_requirement(p, TODAY)
    assert need["lasts"] is False and need["gap"] > 0

    fixed = _short_plan(corpus=p.corpus + need["gap"])
    assert projection.depletion_age(projection.project(fixed, TODAY)) is None

    just_short = _short_plan(corpus=p.corpus + need["gap"] - 100_000)
    assert projection.depletion_age(projection.project(just_short, TODAY)) is not None


def test_surplus_reads_as_a_negative_gap():
    """A plan that already works reports the cushion instead of a shortfall, so
    the same figure answers both 'am I short?' and 'by how much am I clear?'."""
    p = _inputs()                                    # healthy: saving, 20 yrs to go
    need = projection.corpus_requirement(p, TODAY)
    assert need["lasts"] is True
    assert need["gap"] <= 0
    assert need["needed"] < p.corpus or need["needed"] == 0.0


def test_needed_corpus_is_zero_when_savings_alone_carry_it():
    p = _inputs(corpus=0.0, annual_savings=5_000_000.0)
    need = projection.corpus_requirement(p, TODAY)
    assert need["needed"] == 0.0


def test_a_better_return_needs_less_money_today():
    lo = projection.corpus_requirement(_short_plan(return_pct=8.0), TODAY)["needed"]
    mid = projection.corpus_requirement(_short_plan(return_pct=10.0), TODAY)["needed"]
    hi = projection.corpus_requirement(_short_plan(return_pct=12.0), TODAY)["needed"]
    assert lo > mid > hi


def test_a_goal_raises_what_you_need_today():
    without = projection.corpus_requirement(_short_plan(), TODAY)["needed"]
    with_goal = projection.corpus_requirement(
        _short_plan(outflows=((5, 5_000_000.0, "Wedding"),)), TODAY)["needed"]
    assert with_goal > without


def test_band_carries_the_requirement_at_all_three_returns():
    band = projection.project_band(_short_plan(), TODAY)
    # Worse returns -> you need more today. This spread is the decision-useful bit.
    assert band["need_low"]["needed"] > band["need"]["needed"] > band["need_high"]["needed"]


# --- Reading goals off the Goals page ---------------------------------------

def test_outflows_from_goals_maps_dates_to_year_offsets():
    goals = [
        {"name": "College", "target_amount": 5_000_000.0, "target_date": "2031-06-01"},
        {"name": "Marriage", "target_amount": 3_000_000.0, "target_date": "2036-01-01"},
    ]
    out = projection.outflows_from_goals(goals, TODAY, end_age=95, current_age=40)
    assert out == ((5, 5_000_000.0, "College"), (10, 3_000_000.0, "Marriage"))


@pytest.mark.parametrize("goal", [
    {"name": "Undated", "target_amount": 100.0, "target_date": None},
    {"name": "Past", "target_amount": 100.0, "target_date": "2020-01-01"},
    {"name": "Zero", "target_amount": 0.0, "target_date": "2030-01-01"},
    {"name": "Beyond horizon", "target_amount": 100.0, "target_date": "2130-01-01"},
    {"name": "Junk date", "target_amount": 100.0, "target_date": "not-a-date"},
])
def test_outflows_from_goals_skips_what_it_cannot_place(goal):
    assert projection.outflows_from_goals([goal], TODAY, 95, 40) == ()


# --- The page ---------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import prices, storage
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    from fastapi.testclient import TestClient
    import app.main as m
    return TestClient(m.app)


def _login(email="plan@test.com"):
    from datetime import datetime, timedelta
    from app import auth, storage
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def test_plan_page_asks_for_inputs_before_it_has_any(client):
    _uid, ck = _login()
    page = client.get("/plan", cookies=ck).text
    assert "Start with four numbers" in page
    assert "plan-chart" not in page          # nothing to draw yet


def test_age_is_stored_as_a_birth_year_so_it_cannot_go_stale(client):
    from datetime import date
    from app import storage
    uid, ck = _login("age@test.com")
    client.post("/plan/settings", data={"current_age": "40", "retire_age": "60",
                                        "annual_savings": "1200000",
                                        "return_pct": "10", "inflation_pct": "6"},
                cookies=ck, follow_redirects=False)
    s = storage.get_plan_settings(uid)
    assert s["birth_year"] == date.today().year - 40
    assert s["retire_age"] == 60 and s["annual_savings"] == 1_200_000.0


def test_plan_settings_do_not_disturb_the_other_user_settings(client):
    from app import storage
    uid, ck = _login("mix@test.com")
    storage.save_user_settings(uid, "ABCDE1234F", "reg@cams.com")
    storage.save_swr_pct(uid, 2.5)
    client.post("/plan/settings", data={"current_age": "45"}, cookies=ck,
                follow_redirects=False)
    assert storage.get_user_settings(uid) == {"pan": "ABCDE1234F", "cams_email": "reg@cams.com"}
    assert storage.get_swr_pct(uid) == 2.5


def test_plan_renders_a_projection_and_lands_dated_goals_on_it(client):
    from datetime import date
    from app import storage
    uid, ck = _login("full@test.com")
    storage.add_expense(uid, "Living", "housing", 100000.0, "monthly")
    storage.add_bank_cash(uid, "bank-accounts", 20_000_000.0, "HDFC", "savings", "main")
    storage.add_goal(uid, "College", "education", 5_000_000.0,
                     target_date=f"{date.today().year + 8}-06-01")
    client.post("/plan/settings", data={"current_age": "40", "retire_age": "60",
                                        "annual_savings": "1500000",
                                        "return_pct": "10", "inflation_pct": "6"},
                cookies=ck, follow_redirects=False)

    page = client.get("/plan", cookies=ck).text
    assert "plan-chart" in page
    assert "College" in page                       # the goal is listed as an outflow
    assert "retires" in page                       # retirement row is tagged
    # Both ends of the band are reported, so the base case can't read as the answer.
    assert "If returns are 8%" in page and "If returns are 12%" in page
    assert "models no tax at all" in page


def test_plan_shows_what_it_would_take_to_reach_95(client):
    """A depletion age is a diagnosis; the gap is the actionable number."""
    from app import storage
    uid, ck = _login("gap@test.com")
    storage.add_expense(uid, "Living", "housing", 200000.0, "monthly")
    storage.add_bank_cash(uid, "bank-accounts", 5_000_000.0, "HDFC", "savings", "main")
    client.post("/plan/settings", data={"current_age": "60", "retire_age": "60",
                                        "annual_savings": "0", "return_pct": "10",
                                        "inflation_pct": "6"},
                cookies=ck, follow_redirects=False)

    page = client.get("/plan", cookies=ck).text
    assert "To make it to 95 you'd need about" in page
    assert "more today" in page
    assert "a starting corpus of" in page
