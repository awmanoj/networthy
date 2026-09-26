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


def test_schedule_repays_exactly_and_on_time():
    run = h.schedule(7_500_000, 8.5, 20)
    assert run["months"] == 240
    # Total repaid = principal + interest, to the rupee.
    assert run["interest"] == pytest.approx(240 * run["emi"] - 7_500_000, rel=1e-6)


def test_stamp_duty_is_not_covered_by_the_loan():
    """The page's central claim: cash needed is the down payment PLUS duty. RBI
    excludes stamp duty from the value when computing loan-to-value."""
    price = 10_000_000
    loan, _ltv, _band = h.max_loan(price)
    duty = h.duty_for("Karnataka")
    cash = (price - loan) + price * (duty["stamp"] + duty["reg"]) / 100
    assert cash == pytest.approx(3_100_000)         # not the 25,00,000 people budget


def test_every_state_has_a_plausible_rate():
    for row in h.STAMP_DUTY:
        assert 0 < row["stamp"] <= 10 and 0 <= row["reg"] <= 5
        if row["women"] is not None:
            assert row["women"] <= row["stamp"]     # a rebate, never a surcharge


def test_an_unknown_state_falls_back_rather_than_raising():
    assert h.duty_for("Atlantis")["state"] == h.DEFAULT_STATE


# --- Prepayment: the inversion ------------------------------------------------

def test_prepaying_shortens_the_loan_and_cuts_interest():
    base = h.schedule(7_500_000, 8.5, 20)
    early = h.schedule(7_500_000, 8.5, 20, 200_000)
    assert early["months"] < base["months"]
    assert early["interest"] < base["interest"]


@pytest.mark.parametrize("target", [15, 12, 10, 7])
def test_the_ladder_round_trips(target):
    """The test that matters: paying exactly what the page says ends the loan in
    exactly the year it promises, and a little less doesn't."""
    annual = h.prepay_for_target(7_500_000, 8.5, 20, target)
    assert h.schedule(7_500_000, 8.5, 20, annual)["months"] <= target * 12
    assert h.schedule(7_500_000, 8.5, 20, annual * 0.9)["months"] > target * 12


def test_a_target_longer_than_the_loan_needs_nothing():
    assert h.prepay_for_target(7_500_000, 8.5, 20, 25) == 0.0


def test_ladder_rows_are_ordered_and_only_ever_save_money():
    rows = h.ladder(7_500_000, 8.5, 20)
    assert rows[0]["base"] and rows[0]["annual"] == 0.0
    for r in rows[1:]:
        assert r["annual"] > 0 and r["saved"] > 0
        assert r["months"] < rows[0]["months"]


def test_affordability_works_backwards_from_income():
    out = h.affordable_price(200_000, 8.5, 20)
    assert out["emi"] == pytest.approx(100_000)          # 50% FOIR
    # That EMI should service roughly the loan it implies, within rounding.
    assert h.emi(out["loan"], 8.5, 20) == pytest.approx(100_000, rel=1e-6)
    assert out["price"] > out["loan"]                    # a deposit is still needed


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
    table and the prepayment ladder all have to be in the HTML."""
    body = client.get(PATH).text
    assert "₹75,00,000" in body or "7,500,000" in body      # the reference loan
    assert "Kerala" in body and "Tamil Nadu" in body        # the duty table
    assert body.count("hl-pm") >= 3                         # ladder rows, not an empty table
    assert "stamp duty" in body.lower()


def test_the_browser_config_is_valid_json(client):
    """It's rendered into a <script>; `inf` would be valid Python and broken JSON."""
    body = client.get(PATH).text
    cfg = re.search(r"initHomeLoan\((\{.*?\})\);</script>", body, re.S).group(1)
    parsed = json.loads(cfg)
    assert parsed["defaultState"] == h.DEFAULT_STATE
    assert len(parsed["ltv"]) == len(h.LTV_BANDS)
    assert all(isinstance(b[0], (int, float)) for b in parsed["ltv"])


def test_nothing_the_visitor_types_is_sent_anywhere():
    js = open("app/static/homeloan.js").read()
    code = "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))
    assert "fetch(" not in code and "XMLHttpRequest" not in code
