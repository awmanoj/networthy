"""Tests for the by-instrument grid under the Plan.

The one that matters is the first: with every row at the Plan's rate and no
loans, the grid's total is the Plan's balance to the rupee. That invariant is
what lets the simple Plan and the granular grid coexist without ever telling
the user two different futures.
"""

from datetime import date, datetime, timedelta

import pytest

from app import matrix, projection
from app.matrix import Loan, MatrixInputs


TODAY = date(2026, 1, 1)


def _inputs(**over) -> MatrixInputs:
    base = dict(
        current_age=40, retire_age=50, annual_savings=1_000_000.0,
        annual_expense=1_200_000.0, return_pct=10.0, inflation_pct=6.0,
        values={"bank-cash": 1_000_000.0, "fixed-income": 2_000_000.0,
                "mutual-funds": 5_000_000.0, "equity": 3_000_000.0,
                "real-estate": 20_000_000.0},
        outflows=((3, 2_500_000.0, "College"),),
        inflows=((5, 4_000_000.0, "ESOP"),),
        years=20,
    )
    base.update(over)
    return MatrixInputs(**base)


# --- The invariant ----------------------------------------------------------

def test_uniform_rates_reproduce_the_plan_exactly():
    """Same age, savings, burn, goals and windfall; every row at the Plan's rate;
    no loans. The total row *is* the Plan — across retirement and a goal that
    drains the liquid rows."""
    p = _inputs()
    out = matrix.project_matrix(p, TODAY)
    plan = projection.project(projection.PlanInputs(
        current_age=40, retire_age=50, annual_savings=1_000_000.0,
        corpus=31_000_000.0, annual_expense=1_200_000.0, return_pct=10.0,
        inflation_pct=6.0, outflows=p.outflows, inflows=p.inflows, end_age=59,
    ), TODAY)
    assert len(out.years) == 20 and out.years[0] == 2026 and out.ages[-1] == 59
    for i, row in enumerate(plan):
        assert out.net_nominal[i] == pytest.approx(row.closing, rel=1e-9)
        assert out.net_real[i] == pytest.approx(row.real_closing, rel=1e-9)
        assert out.plan_nominal[i] == pytest.approx(row.closing, rel=1e-9)
    assert not any(out.shortfall)


def test_uniform_rates_still_match_when_the_corpus_runs_dry():
    """The Plan clamps at zero; the grid simply has nothing left to draw.
    Both read as zero, and the grid flags the year the draw couldn't be met."""
    p = _inputs(values={"bank-cash": 500_000.0}, annual_savings=0.0,
                retire_age=40, outflows=(), inflows=(), years=10)
    out = matrix.project_matrix(p, TODAY)
    plan = projection.project(projection.PlanInputs(
        current_age=40, retire_age=40, annual_savings=0.0, corpus=500_000.0,
        annual_expense=1_200_000.0, return_pct=10.0, inflation_pct=6.0, end_age=49,
    ), TODAY)
    for i, row in enumerate(plan):
        assert out.net_nominal[i] == pytest.approx(row.closing, abs=1e-6)
    assert out.shortfall[0] and out.net_nominal[0] == 0.0


# --- Per-row rates ----------------------------------------------------------

def test_a_row_rate_moves_only_that_row():
    base = matrix.project_matrix(_inputs(), TODAY)
    tuned = matrix.project_matrix(_inputs(rates={"real-estate": 6.0}), TODAY)
    by_slug = lambda r: {row.slug: row for row in r.assets}   # noqa: E731
    b, t = by_slug(base), by_slug(tuned)
    assert t["real-estate"].rate_pct == 6.0 and b["real-estate"].rate_pct == 10.0
    assert t["real-estate"].values[0] == pytest.approx(20_000_000.0 * 1.06)
    assert b["real-estate"].values[0] == pytest.approx(20_000_000.0 * 1.10)
    # Rows nobody touched, and that nothing was drawn from, are identical.
    for slug in ("mutual-funds", "equity"):
        assert t[slug].values[:3] == b[slug].values[:3]
    # And the grid now disagrees with the Plan, which is the point of editing.
    assert tuned.net_nominal[-1] < tuned.plan_nominal[-1]
    assert base.net_nominal[-1] == pytest.approx(base.plan_nominal[-1], rel=1e-9)


def test_unknown_rate_slugs_are_ignored_and_missing_ones_default():
    out = matrix.project_matrix(_inputs(rates={"nonsense": 50.0}), TODAY)
    assert all(r.rate_pct == 10.0 for r in out.assets)


# --- Where money goes -------------------------------------------------------

def test_savings_and_windfalls_land_in_the_chosen_row():
    out = matrix.project_matrix(_inputs(savings_slug="equity", inflows=((0, 1_000_000.0, "Bonus"),)), TODAY)
    eq = next(r for r in out.assets if r.slug == "equity")
    mf = next(r for r in out.assets if r.slug == "mutual-funds")
    assert eq.is_savings and not mf.is_savings
    # 3cr*1.1 + 10L savings + 10L bonus, nothing drawn in year 0.
    assert eq.values[0] == pytest.approx(3_000_000.0 * 1.1 + 2_000_000.0)
    assert mf.values[0] == pytest.approx(5_000_000.0 * 1.1)
    assert out.inflows[0] == 1_000_000.0 and "+Bonus" in out.flow_labels[0]


def test_a_bad_savings_slug_falls_back_to_the_default():
    out = matrix.project_matrix(_inputs(savings_slug="moon"), TODAY)
    assert next(r for r in out.assets if r.is_savings).slug == matrix.DEFAULT_SAVINGS_SLUG


def test_draws_come_from_the_most_liquid_rows_first():
    """A ₹25L goal in year 3 empties cash, then dips into fixed income. Real
    estate is never touched."""
    p = _inputs(annual_savings=0.0, inflows=(), outflows=((0, 2_500_000.0, "College"),))
    out = matrix.project_matrix(p, TODAY)
    rows = {r.slug: r for r in out.assets}
    assert rows["bank-cash"].values[0] == 0.0                       # 11L, all taken
    assert rows["fixed-income"].values[0] == pytest.approx(2_200_000.0 - 1_400_000.0)
    assert rows["mutual-funds"].values[0] == pytest.approx(5_500_000.0)
    assert rows["real-estate"].values[0] == pytest.approx(22_000_000.0)
    assert out.outflows[0] == 2_500_000.0 and out.flow_labels[0] == ("College",)


def test_illiquid_rows_are_drawn_only_when_nothing_else_is_left():
    p = _inputs(values={"bank-cash": 100_000.0, "real-estate": 10_000_000.0},
                annual_savings=0.0, inflows=(), outflows=((0, 1_000_000.0, "Car"),))
    out = matrix.project_matrix(p, TODAY)
    rows = {r.slug: r for r in out.assets}
    assert rows["bank-cash"].values[0] == 0.0
    assert rows["real-estate"].values[0] == pytest.approx(11_000_000.0 - 890_000.0)
    assert not out.shortfall[0]


def test_expenses_are_drawn_only_after_retirement():
    out = matrix.project_matrix(_inputs(retire_age=42, outflows=(), inflows=()), TODAY)
    assert out.expenses[0] == 0.0 and out.expenses[1] == 0.0
    assert out.expenses[2] == pytest.approx(1_200_000.0 * 1.06 ** 2)
    assert out.savings[1] == pytest.approx(1_000_000.0 * 1.06) and out.savings[2] == 0.0
    assert out.retired == [False, False] + [True] * 18


# --- Loans ------------------------------------------------------------------

def test_a_loan_with_an_emi_amortises_like_the_bank_does():
    loan = Loan("HDFC home loan", "home-loan", 5_000_000.0, rate_pct=9.0, emi=50_000.0)
    out = matrix.project_matrix(_inputs(loans=(loan,), outflows=(), inflows=()), TODAY)
    bal = 5_000_000.0
    for _ in range(12):
        bal = bal * (1 + 0.09 / 12) - 50_000.0
    assert out.loans[0].values[0] == pytest.approx(bal)
    assert out.loans[0].method == "from its EMI"
    assert out.loans_total.now == 5_000_000.0
    assert out.net_now == pytest.approx(31_000_000.0 - 5_000_000.0)
    # It subtracts from net worth and eventually reaches zero.
    assert out.net_nominal[0] == pytest.approx(out.assets_total.values[0] - bal)
    assert out.loans[0].values[-1] == 0.0


def test_a_loan_is_gone_once_its_end_date_has_passed():
    loan = Loan("Car", "vehicle-loan", 600_000.0, rate_pct=10.0, emi=1.0,
                end_date=date(2027, 6, 30))
    out = matrix.project_matrix(_inputs(loans=(loan,)), TODAY)
    assert out.loans[0].values[0] > 0            # still running through 2026
    assert out.loans[0].values[1] == 0.0         # ended mid-2027


def test_a_loan_with_only_an_end_date_runs_straight_to_zero():
    loan = Loan("Personal", "personal-loan", 480_000.0, end_date=date(2029, 12, 31))
    out = matrix.project_matrix(_inputs(loans=(loan,)), TODAY)
    # 48 months left at the start of 2026: a quarter comes off each year.
    assert out.loans[0].values[0] == pytest.approx(360_000.0)
    assert out.loans[0].values[1] == pytest.approx(240_000.0)
    assert out.loans[0].values[3] == 0.0
    assert out.loans[0].method == "straight line to its end date"


def test_a_loan_with_nothing_to_go_on_is_held_flat_and_says_so():
    loan = Loan("Credit card", "credit-card", 80_000.0)
    out = matrix.project_matrix(_inputs(loans=(loan,)), TODAY)
    assert all(v == 80_000.0 for v in out.loans[0].values)
    assert "add an EMI or end date" in out.loans[0].method


def test_a_valuation_adjustment_never_amortises():
    rows = matrix.loans_from_rows({
        "valuation-adjustment": [{"lender": "AIF haircut", "outstanding": 1_500_000.0,
                                  "rate": None, "emi": 10_000.0, "end_date": "2027-01-01"}],
        "home-loan": [{"lender": "SBI", "outstanding": 100.0, "rate": 8.0, "emi": 10.0,
                       "end_date": None}],
    })
    assert rows[0].amortise is False and rows[0].method == "held flat"
    assert rows[1].amortise is True and rows[1].end_date is None
    out = matrix.project_matrix(_inputs(loans=rows), TODAY)
    assert all(v == 1_500_000.0 for v in out.loans[0].values)


# --- Plumbing ---------------------------------------------------------------

def test_instrument_values_collapse_the_tree_onto_rows():
    tree = {
        "assets/financial-assets/bank-cash": 10.0,
        "assets/financial-assets/foreign-exchange": 5.0,
        "assets/financial-assets/gold-silver": 7.0,
        "assets/non-financial-assets/physical-gold": 3.0,
        "assets/non-financial-assets/real-estate": 100.0,
    }
    v = matrix.instrument_values(tree)
    assert v["bank-cash"] == 15.0 and v["gold-silver"] == 10.0
    assert v["real-estate"] == 100.0 and v["crypto"] == 0.0


def test_locked_retirement_schemes_are_not_drawn_before_market_rows():
    """PPF and EPF are filed under fixed income in the tree, but a goal can't be
    paid from them. They sit behind mutual funds and equity in the draw order."""
    order = list(matrix.DRAW_ORDER)
    assert order.index("fixed-income") < order.index("mutual-funds")
    assert order.index("retirement-savings") > order.index("equity")
    assert order.index("retirement-savings") < order.index("real-estate")
    p = _inputs(values={"retirement-savings": 5_000_000.0, "mutual-funds": 1_000_000.0},
                annual_savings=0.0, inflows=(), outflows=((0, 500_000.0, "Car"),))
    out = matrix.project_matrix(p, TODAY)
    rows = {r.slug: r for r in out.assets}
    assert rows["mutual-funds"].values[0] == pytest.approx(1_100_000.0 - 500_000.0)
    assert rows["retirement-savings"].values[0] == pytest.approx(5_500_000.0)
    tree = {"assets/financial-assets/fixed-income/ppf": 1.0,
            "assets/financial-assets/fixed-income/fixed-deposits": 2.0}
    v = matrix.instrument_values(tree)
    assert v["retirement-savings"] == 1.0 and v["fixed-income"] == 2.0


def test_every_fixed_income_leaf_is_on_exactly_one_row():
    """A new leaf under Fixed Income must be filed as liquid or locked here."""
    from app import networth
    chain = networth.resolve("assets/financial-assets/fixed-income")
    leaves = {f"assets/financial-assets/fixed-income/{c.slug}" for c in chain[-1].children}
    covered = [path for inst in matrix.INSTRUMENTS for path in inst.paths if path in leaves]
    assert sorted(covered) == sorted(leaves)


def test_every_instrument_path_exists_in_the_tree():
    """A renamed tree node must fail here, not silently read as zero forever."""
    from app import networth
    for inst in matrix.INSTRUMENTS:
        for path in inst.paths:
            assert networth.resolve(path), f"{inst.slug}: {path} is not in the tree"
    assert matrix.DEFAULT_SAVINGS_SLUG in matrix.INSTRUMENT_BY_SLUG


def test_horizon_is_capped_at_the_plans_end_age():
    out = matrix.project_matrix(_inputs(current_age=90, years=30), TODAY)
    assert len(out.years) == projection.END_AGE - 90


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


def _login(email="grid@test.com"):
    from app import auth, storage
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _set_up_plan(client, ck):
    client.post("/plan/settings", data={"current_age": "40", "retire_age": "60",
                                        "annual_savings": "1500000",
                                        "return_pct": "10", "inflation_pct": "6"},
                cookies=ck, follow_redirects=False)


def test_grid_needs_a_plan_first(client):
    _uid, ck = _login()
    page = client.get("/plan/by-instrument", cookies=ck).text
    assert "set up your Plan" in page and 'href="/plan"' in page
    assert "matrix-table" not in page


def test_grid_renders_rows_loans_and_the_plan_reference(client):
    from app import storage
    uid, ck = _login()
    storage.add_bank_cash(uid, "bank-accounts", 2_000_000.0, "HDFC", "savings", "main")
    storage.add_property_holding(uid, "primary-residence", "Flat", 15_000_000.0)
    storage.add_liability(uid, "home-loan", "SBI", 5_000_000.0, principal=6_000_000.0,
                          rate=8.5, emi=55_000.0, end_date="2036-03-31")
    storage.add_goal(uid, "College", "education", 2_500_000.0, target_date="2031-06-01")
    storage.add_goal(uid, "ESOP vesting", "wealth", 3_000_000.0,
                     target_date="2029-04-01", kind="inflow")
    _set_up_plan(client, ck)

    page = client.get("/plan/by-instrument", cookies=ck).text
    assert "matrix-table" in page
    assert "Bank &amp; Cash" in page and "Mutual Funds" in page   # the savings row shows even at zero
    assert "SBI" in page and "from its EMI" in page
    assert "What the Plan says" in page
    assert "+ESOP vesting" in page and "College" in page
    assert 'name="rate_mutual-funds"' in page


def test_rates_and_savings_row_are_saved_and_shown(client):
    from app import storage
    uid, ck = _login()
    storage.add_bank_cash(uid, "bank-accounts", 2_000_000.0, "HDFC", "savings", "main")
    _set_up_plan(client, ck)
    r = client.post("/plan/by-instrument", cookies=ck, follow_redirects=False, data={
        "rate_real-estate": "6", "rate_mutual-funds": "12", "rate_equity": "",
        "savings_slug": "equity", "years": "30",
    })
    assert r.status_code == 303 and r.headers["location"].endswith("years=30")
    assert storage.get_plan_rates(uid) == {"real-estate": 6.0, "mutual-funds": 12.0}
    assert storage.get_plan_settings(uid)["savings_slug"] == "equity"
    # The Plan's own inputs were not disturbed by saving the grid's.
    assert storage.get_plan_settings(uid)["return_pct"] == 10.0

    page = client.get("/plan/by-instrument?years=30", cookies=ck).text
    assert 'name="rate_real-estate" type="number" step="0.1" min="0" value="6"' in page
    assert '<option value="equity" selected' in page
    assert '<option value="30" selected' in page

    # Clearing a rate removes the override rather than storing a blank.
    client.post("/plan/by-instrument", cookies=ck, follow_redirects=False,
                data={"rate_real-estate": "", "savings_slug": "equity", "years": "20"})
    assert storage.get_plan_rates(uid) == {"mutual-funds": 12.0}


def test_grid_rejects_a_savings_row_that_is_not_an_instrument(client):
    from app import storage
    uid, ck = _login()
    _set_up_plan(client, ck)
    client.post("/plan/by-instrument", cookies=ck, follow_redirects=False,
                data={"savings_slug": "sessions; DROP TABLE", "years": "20"})
    assert storage.get_plan_settings(uid)["savings_slug"] is None


def test_plan_page_links_to_the_grid(client):
    _uid, ck = _login()
    _set_up_plan(client, ck)
    page = client.get("/plan", cookies=ck).text
    assert 'href="/plan/by-instrument"' in page
