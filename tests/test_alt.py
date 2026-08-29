"""Tests for alternate-investment gain enrichment."""

from app.main import _enrich_alt


def test_enrich_alt_gain_from_cost():
    rows = [{"cost": 500000.0, "current_value": 2500000.0, "invested_date": "2022-04-01"}]
    _enrich_alt(rows)
    assert rows[0]["gain_pct"] == 400.0        # 5x -> +400%
    assert rows[0]["signal"] == "up"
    assert rows[0]["invested_fmt"] == "Apr 2022"


def test_enrich_alt_no_cost_means_no_gain():
    rows = [{"cost": None, "current_value": 800000.0, "invested_date": None}]
    _enrich_alt(rows)
    assert rows[0]["gain_pct"] is None and rows[0]["signal"] is None
    assert rows[0]["invested_fmt"] is None


def test_enrich_alt_loss():
    rows = [{"cost": 1000000.0, "current_value": 400000.0, "invested_date": None}]
    _enrich_alt(rows)
    assert rows[0]["gain_pct"] == -60.0
    assert rows[0]["signal"] == "down"

