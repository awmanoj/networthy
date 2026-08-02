"""Tests for liability enrichment (% paid off, remaining tenure) and net-worth sign."""

from datetime import date, timedelta

from app.main import _enrich_liability
from app.networth import LIABILITY_LEAVES, resolve


def test_liability_leaves_match_the_tree():
    secured = {c.slug for c in resolve("liabilities/secured-loans")[-1].children}
    unsecured = {c.slug for c in resolve("liabilities/unsecured-loans")[-1].children}
    assert LIABILITY_LEAVES == secured | unsecured


def test_paid_off_from_principal_and_outstanding():
    rows = [{"principal": 5000000.0, "outstanding": 3500000.0, "end_date": None}]
    _enrich_liability(rows)
    assert rows[0]["paid_pct"] == 30.0   # (5,000,000 - 3,500,000)/5,000,000


def test_no_principal_means_no_paid_off():
    rows = [{"principal": None, "outstanding": 45000.0, "end_date": None}]
    _enrich_liability(rows)
    assert rows[0]["paid_pct"] is None


def test_remaining_tenure_from_end_date():
    future = (date.today() + timedelta(days=800)).isoformat()
    past = (date.today() - timedelta(days=10)).isoformat()
    rows = [
        {"principal": None, "outstanding": 1.0, "end_date": future},
        {"principal": None, "outstanding": 1.0, "end_date": past},
    ]
    _enrich_liability(rows)
    assert rows[0]["remaining"].endswith("yrs left")
    assert rows[1]["remaining"] == "ended"
