"""Tests for manual-entry date parsing and tenure computation."""

from app.main import _enrich_manual, _opt_date, _years_label


def test_opt_date_validates_iso():
    assert _opt_date("2038-04-01") == "2038-04-01"
    assert _opt_date("  2023-01-15 ") == "2023-01-15"  # trimmed
    assert _opt_date("") is None
    assert _opt_date("01/04/2038") is None             # not ISO
    assert _opt_date("not a date") is None


def test_years_label():
    assert _years_label(15.0) == "15 yrs"
    assert _years_label(1.0) == "1 yr"
    assert _years_label(15.02) == "15 yrs"  # near-integer snaps
    assert _years_label(1.5) == "1.5 yrs"


def test_enrich_manual_computes_tenure_from_dates():
    rows = [{"investment_date": "2023-04-01", "maturity_date": "2038-04-01", "years": None}]
    _enrich_manual(rows)
    assert rows[0]["tenure"] == "15 yrs"
    assert rows[0]["invested_fmt"] == "01 Apr 2023"
    assert rows[0]["matures_fmt"] == "01 Apr 2038"


def test_enrich_manual_falls_back_to_legacy_years():
    rows = [{"investment_date": None, "maturity_date": None, "years": 5.0}]
    _enrich_manual(rows)
    assert rows[0]["tenure"] == "5 yrs"
    assert rows[0]["invested_fmt"] is None
    assert rows[0]["matures_in"] is None


def test_enrich_manual_flags_matured():
    rows = [{"investment_date": None, "maturity_date": "2000-01-01", "years": None}]
    _enrich_manual(rows)
    assert rows[0]["matures_in"] == "matured"
    assert rows[0]["tenure"] is None  # no investment date to measure from
