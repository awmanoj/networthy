"""The home-buying page: RBI's lending caps, the cash nobody budgets for, and
how much a year it takes to finish a 20-year loan in 10."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import homeloan as h
from app import prices, storage

PATH = "/how-much-house-can-i-afford"


# --- RBI loan-to-value slabs --------------------------------------------------

@pytest.mark.parametrize("price,expect_ltv", [
    (2_500_000, 0.90),     # loan 22.5L — inside the ≤30L band
    (3_300_000, 0.90),     # loan 29.7L — still inside it
    (3_500_000, 0.80),     # 90% would be 31.5L, past the band, so drops to 80%
    (9_000_000, 0.80),     # loan 72L — inside the 30–75L band
    (15_000_000, 0.75),    # 80% would be 1.2cr, past it, so 75%
])
def test_ltv_slab_picks_the_band_the_loan_actually_falls_in(price, expect_ltv):
    """The slabs are defined on the loan, not the price, so it's circular: the
    most generous band whose resulting loan fits inside it is the answer."""
    _loan, ltv, _band = h.max_loan(price)
    assert ltv == expect_ltv


# --- The EMI, and the thing everyone forgets ---------------------------------

def test_emi_matches_the_standard_formula():
    # ₹75L at 8.5% over 20 years is a widely quoted figure; ~₹65,087.
    assert h.emi(7_500_000, 8.5, 20) == pytest.approx(65086.74, abs=0.5)


def test_zero_rate_does_not_divide_by_zero():
    assert h.emi(1_200_000, 0.0, 10) == pytest.approx(10_000.0)


def test_payoff_at_the_scheduled_emi_repays_exactly_and_on_time():
    e = h.emi(7_500_000, 8.5, 20)
    run = h.payoff(7_500_000, 8.5, e)
    assert run["months"] == 240
    # Total repaid = principal + interest, to the rupee.
    assert run["interest"] == pytest.approx(240 * e - 7_500_000, rel=1e-6)


def test_stamp_duty_is_not_covered_by_the_loan():
    """The page's central claim: cash needed is the down payment PLUS duty. RBI
    excludes stamp duty from the value when computing loan-to-value."""
    price = 10_000_000
    loan, _ltv, _band = h.max_loan(price)
    cash = (price - loan) + price * h.effective_pct(h.duty_for(h.DEFAULT_STATE)) / 100
    assert cash == pytest.approx(3_260_000)         # not the 25,00,000 people budget


def test_karnataka_includes_its_cess_and_surcharge():
    """Verified Oct 2026: 5% stamp, +10% cess and +2% urban surcharge *on the
    duty*, +2% registration (doubled from 1% in Aug 2025) = 7.6%.

    The old model had only `stamp` and `reg` and so read 6% — understating the
    cash a Bengaluru buyer needs by ₹1.6 lakh on a ₹1 crore flat, which is
    precisely the miss this page exists to prevent."""
    k = h.duty_for("Karnataka (urban/BBMP)")
    assert h.effective_pct(k) == pytest.approx(7.6)
    assert k["verified"]


def test_a_state_with_no_cess_is_just_stamp_plus_registration():
    plain = {"stamp": 6.0, "cess_pct": 0.0, "surcharge_pct": 0.0, "reg": 1.0}
    assert h.effective_pct(plain) == pytest.approx(7.0)


def test_unverified_states_are_flagged_as_such():
    """Rates move with state budgets. A row nobody has checked must not be shown
    with the same confidence as one that has."""
    assert any(d["verified"] for d in h.STAMP_DUTY)
    assert any(not d["verified"] for d in h.STAMP_DUTY)


def test_every_state_has_a_plausible_rate():
    for row in h.STAMP_DUTY:
        assert 0 < row["stamp"] <= 10 and 0 <= row["reg"] <= 5
        assert 4.0 <= h.effective_pct(row) <= 12.0
        if row["women"] is not None:
            assert row["women"] <= row["stamp"]     # a rebate, never a surcharge


def test_an_unknown_state_falls_back_rather_than_raising():
    assert h.duty_for("Atlantis")["state"] == h.DEFAULT_STATE


# --- Prepayment: the inversion ------------------------------------------------

E75 = h.emi(7_500_000, 8.5, 20)


def test_prepaying_shortens_the_loan_and_cuts_interest():
    base = h.payoff(7_500_000, 8.5, E75)
    early = h.payoff(7_500_000, 8.5, E75, monthly_prepay=15_000)
    assert early["months"] < base["months"]
    assert early["interest"] < base["interest"]


@pytest.mark.parametrize("target", [15, 12, 10, 7])
def test_the_ladder_round_trips(target):
    """The test that matters: paying exactly what the page says a month ends
    the loan in exactly the year it promises, and a little less doesn't."""
    monthly = h.prepay_for_target(7_500_000, 8.5, E75, target)
    assert h.payoff(7_500_000, 8.5, E75, monthly)["months"] <= target * 12
    assert h.payoff(7_500_000, 8.5, E75, monthly * 0.9)["months"] > target * 12


def test_the_ladder_respects_what_is_parked():
    """Same loop as everything else: with ₹5 lakh parked, less extra per month
    is needed to hit the same target, and the round trip still holds."""
    plain = h.prepay_for_target(5_000_000, 8.5, h.emi(5_000_000, 8.5, 15), 10)
    parked = h.prepay_for_target(5_000_000, 8.5, h.emi(5_000_000, 8.5, 15), 10, parked=500_000)
    assert 0 < parked < plain
    run = h.payoff(5_000_000, 8.5, h.emi(5_000_000, 8.5, 15), parked, parked=500_000)
    assert run["months"] <= 120


def test_a_target_longer_than_the_loan_needs_nothing():
    assert h.prepay_for_target(7_500_000, 8.5, E75, 25) == 0.0


def test_an_emi_that_does_not_cover_interest_has_no_ladder():
    """The base row would be "never", so there is nothing to shorten from."""
    assert h.ladder(7_500_000, 8.5, 10_000) == []


def test_ladder_rows_are_ordered_and_only_ever_save_money():
    rows = h.ladder(7_500_000, 8.5, E75)
    assert rows[0]["base"] and rows[0]["monthly"] == 0.0
    for r in rows[1:]:
        assert r["monthly"] > 0 and r["saved"] > 0
        assert r["months"] < rows[0]["months"]
    assert [r["months"] for r in rows] == sorted((r["months"] for r in rows), reverse=True)


def test_the_faq_prose_matches_the_ladder():
    """`_HOME_FAQ` states the 15-year figure in words. It's a hand-typed number,
    so pin it to the arithmetic it claims to describe."""
    import app.main as m
    fifteen = next(r for r in h.ladder(7_500_000, 8.5, E75) if r["months"] == 180)
    assert 8_700 <= fifteen["monthly"] <= 8_900             # "about ₹8,800 extra a month"
    assert fifteen["saved"] > 2_300_000                      # "saves over ₹23 lakh"
    faq = next(f for f in m._HOME_FAQ if f["q"].startswith("Does prepaying"))["a"]
    assert "₹8,800" in faq and "₹23 lakh" in faq


def test_affordability_works_backwards_from_income():
    out = h.affordable_price(200_000, 8.5, 20)
    assert out["emi"] == pytest.approx(100_000)          # 50% FOIR
    # That EMI should service roughly the loan it implies, within rounding.
    assert h.emi(out["loan"], 8.5, 20) == pytest.approx(100_000, rel=1e-6)
    assert out["price"] > out["loan"]                    # a deposit is still needed


# --- The sanction letter beats the slab --------------------------------------

def test_without_a_sanction_the_rbi_cap_is_the_loan():
    out = h.loan_amount(10_000_000)
    assert out["loan"] == 7_500_000 and not out["sanctioned"] and not out["over_cap"]
    assert out["cap"] == out["loan"]


def test_a_sanction_below_the_cap_is_the_loan_and_raises_the_cash_needed():
    """A bank lends its slab on *its* valuation, not the agreement value. The
    slab is still reported, as what the ceiling would have been."""
    out = h.loan_amount(10_000_000, 6_500_000)
    assert out["loan"] == 6_500_000 and out["sanctioned"]
    assert out["ltv"] == pytest.approx(0.65)
    assert out["cap"] == 7_500_000 and not out["over_cap"]


def test_a_sanction_above_the_cap_is_flagged_and_never_exceeds_the_price():
    assert h.loan_amount(3_500_000, 3_300_000)["over_cap"]          # cap is 80% = 28L
    out = h.loan_amount(10_000_000, 12_000_000)
    assert out["loan"] == 10_000_000 and out["over_cap"]


def test_a_zero_or_blank_sanction_means_none():
    assert h.loan_amount(10_000_000, 0) == h.loan_amount(10_000_000, None)


# --- Closing a loan you already have -----------------------------------------

def test_a_blank_emi_means_the_scheduled_one():
    """The page's EMI field is optional; blank must be the bank's figure, so the
    scenario run with nothing set is the plain amortisation."""
    s = h.payoff_scenarios(5_000_000, 8.5, 15)
    assert s["emi_paid"] == pytest.approx(h.emi(5_000_000, 8.5, 15))
    assert s["combined"]["months"] == 180


def test_parked_money_shortens_the_loan_like_a_prepayment_of_the_same_size():
    """The MaxGain claim: interest is charged on balance − parked, so ₹5 lakh in
    the OD account ends the loan when ₹5 lakh paid off the principal would."""
    sched = h.emi(5_000_000, 8.5, 15)
    parked = h.payoff(5_000_000, 8.5, sched, parked=500_000)
    prepaid = h.payoff(4_500_000, 8.5, sched)
    assert parked["months"] == prepaid["months"]
    assert parked["interest"] == pytest.approx(prepaid["interest"])
    assert parked["net_start"] == 4_500_000


LEVERS = dict(balance=5_000_000, rate_pct=8.5, years_left=15,
              emi_paid=55_000, monthly_prepay=10_000, parked=500_000)


def test_each_lever_shortens_the_loan_and_all_three_shorten_it_most():
    s = h.payoff_scenarios(**LEVERS)
    base, *levers, combined = s["rows"]
    assert s["levers"] == 3 and len(levers) == 3
    for r in levers:
        assert r["closes"] and r["months"] < base["months"] and r["saved"] > 0
    assert combined["months"] < min(r["months"] for r in levers)
    assert combined["saved"] > max(r["saved"] for r in levers)
    assert combined["interest"] == pytest.approx(s["combined"]["interest"])


def test_a_higher_emi_and_a_monthly_prepayment_are_the_same_rupees():
    """Kept as separate inputs because people decide them separately — but the
    arithmetic must not pretend they differ."""
    sched = h.emi(5_000_000, 8.5, 15)
    a = h.payoff(5_000_000, 8.5, sched + 10_000)
    b = h.payoff(5_000_000, 8.5, sched, monthly_prepay=10_000)
    assert a["months"] == b["months"] and a["interest"] == pytest.approx(b["interest"])


def test_an_emi_that_does_not_cover_the_interest_never_closes():
    """₹50 lakh at 8.5% accrues ~₹35,400 a month. Saying "never" is more useful
    than a figure in the thousands of months, and `months` is None, not a number
    the page could mistake for a date."""
    run = h.payoff(5_000_000, 8.5, 30_000)
    assert not run["closes"] and run["months"] is None and run["interest"] is None
    s = h.payoff_scenarios(5_000_000, 8.5, 15, emi_paid=30_000)
    assert s["rows"][1]["closes"] is False and s["rows"][1]["saved"] is None
    assert s["combined"]["closes"] is False


def test_the_yearly_schedule_adds_up():
    """One row per year; principal sums to the balance, interest to the total,
    each row's opening is the previous closing, and it ends at zero."""
    e = h.emi(7_500_000, 8.5, 20)
    run = h.payoff(7_500_000, 8.5, e)
    ys = run["yearly"]
    assert len(ys) == 20 and [y["year"] for y in ys] == list(range(1, 21))
    assert sum(y["principal"] for y in ys) == pytest.approx(7_500_000)
    assert sum(y["interest"] for y in ys) == pytest.approx(run["interest"])
    assert ys[0]["opening"] == 7_500_000 and ys[-1]["closing"] == pytest.approx(0, abs=0.01)
    for prev, cur in zip(ys, ys[1:]):
        assert cur["opening"] == pytest.approx(prev["closing"])
        assert cur["paid"] == pytest.approx(cur["interest"] + cur["principal"])
    assert all(y["paid"] == pytest.approx(12 * e) for y in ys[:-1])


def test_a_partial_final_year_is_its_own_row_and_parked_shows_as_net():
    e = h.emi(5_000_000, 8.5, 15)
    run = h.payoff(5_000_000, 8.5, e, parked=500_000)
    ys = run["yearly"]
    assert len(ys) == -(-run["months"] // 12)                # ceil
    assert ys[0]["opening"] == 5_000_000                     # book balance, not net
    assert ys[0]["net"] == pytest.approx(ys[0]["closing"] - 500_000)
    assert ys[-1]["net"] == pytest.approx(0, abs=0.01)
    assert ys[-1]["closing"] == pytest.approx(500_000, abs=0.01)   # what's parked is yours again
    # Every month pays the EMI except the last, which pays only what's left.
    paid = sum(y["paid"] for y in ys)
    assert (run["months"] - 1) * e < paid <= run["months"] * e + 0.01


def test_a_loan_that_never_closes_has_no_schedule():
    assert h.payoff(5_000_000, 8.5, 30_000)["yearly"] == []


def test_fully_parked_balance_closes_immediately():
    run = h.payoff(6_000_000, 8.0, 50_000, parked=6_000_000)
    assert run["closes"] and run["months"] == 0 and run["interest"] == 0.0
    assert run["yearly"] == []


def test_scenario_rows_list_only_the_levers_actually_set():
    """No lever → just the baseline. One lever → its row and no "together" row,
    which would only repeat it. A blank EMI means the scheduled one."""
    none = h.payoff_scenarios(5_000_000, 8.5, 15)
    assert [r["label"] for r in none["rows"]] == ["Scheduled EMI, nothing extra"]
    assert none["levers"] == 0 and none["emi_paid"] == pytest.approx(none["scheduled_emi"])

    one = h.payoff_scenarios(5_000_000, 8.5, 15, parked=500_000)
    assert [r["label"] for r in one["rows"]] == ["Scheduled EMI, nothing extra",
                                                 "Parked in the OD account alone"]
    assert one["levers"] == 1
    assert one["combined"]["months"] == one["rows"][1]["months"]

    two = h.payoff_scenarios(5_000_000, 8.5, 15, emi_paid=55_000, monthly_prepay=10_000)
    assert two["rows"][-1]["label"] == "All of the above together" and two["levers"] == 2


def test_an_emi_equal_to_the_scheduled_one_is_not_a_lever():
    sched = h.emi(5_000_000, 8.5, 15)
    s = h.payoff_scenarios(5_000_000, 8.5, 15, emi_paid=sched + 0.2)
    assert s["levers"] == 0 and len(s["rows"]) == 1


# --- The page -----------------------------------------------------------------

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


def test_page_is_public_and_indexable(client):
    assert client.get(PATH, follow_redirects=False).status_code == 200
    assert PATH in client.get("/sitemap.xml").text
    assert f"Allow: {PATH}" in client.get("/robots.txt").text


def test_the_numbers_are_server_rendered_for_a_crawler(client):
    """A crawler runs no JavaScript. The worked example, the RBI slabs, the duty
    table, the lever table, the ladder and the schedule all have to be in the
    HTML — and they describe the page's own default state, so what the crawler
    reads is what a visitor first sees."""
    body = client.get(PATH).text
    assert "7,500,000" in body                              # the reference loan
    assert "Kerala" in body and "Tamil Nadu" in body        # the duty table
    assert body.count("hl-pm") >= 3                         # ladder rows, not an empty table
    assert "stamp duty" in body.lower()
    assert "Scheduled EMI, nothing extra" in body           # the lever table's baseline
    assert "Payment schedule, year by year" in body
    sched = body[body.index('id="sched-rows"'):body.index('id="sched-note"')]
    assert sched.count("<tr>") == 20                        # one row per year of the reference loan
    assert h.PAYOFF_EXAMPLE["balance"] == 7_500_000 and h.PAYOFF_EXAMPLE["emi_paid"] is None


def test_the_sanction_example_is_server_rendered_and_matches_the_faq(client):
    """The FAQ states the sanction example in prose; the template computes it.
    Both must describe the same flat."""
    body = client.get(PATH).text
    assert 'id="hl-sanction"' in body and 'id="hl-amount"' in body
    assert 'id="hl-mode-have"' in body                      # the buying / have-a-loan pill
    valued = 9_000_000
    loan = h.loan_amount(10_000_000, h.max_loan(valued)[0])["loan"]
    assert "{:,.0f}".format(loan) in body                       # 7,200,000
    assert "₹72 lakh" in body and "₹35.6 lakh" in body


def test_the_browser_config_is_valid_json(client):
    """It's rendered into a <script>; `inf` would be valid Python and broken JSON."""
    body = client.get(PATH).text
    cfg = re.search(r"initHomeLoan\((\{.*?\})\);</script>", body, re.S).group(1)
    parsed = json.loads(cfg)
    assert parsed["defaultState"] == h.DEFAULT_STATE
    assert len(parsed["ltv"]) == len(h.LTV_BANDS)
    assert all(isinstance(b[0], (int, float)) for b in parsed["ltv"])
    assert parsed["payoffMaxMonths"] == h.PAYOFF_MAX_MONTHS


def test_nothing_the_visitor_types_is_sent_anywhere():
    js = open("app/static/homeloan.js").read()
    code = "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))
    assert "fetch(" not in code and "XMLHttpRequest" not in code
