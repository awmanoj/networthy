"""Tests for the CAMS CAS text extraction.

Exercises the scheme/valuation parsing against a representative CAMS statement
snippet (the fragile part), without needing a real password-protected PDF — the
same approach as test_parser.py for the NSDL parser.
"""

from datetime import date

import pytest

from app.parser.cams_cas import _find_statement_date, _parse_schemes

# A CAMS/KFintech detailed CAS, as pdfplumber would flatten a few scheme blocks:
# a plain equity fund, a gold fund, and a silver ETF FoF.
_SAMPLE = """\
Consolidated Account Statement
01-Apr-2023 To 30-Jun-2024
PAN: ABCDE1234F   Email: investor@example.com

HDFC Mutual Fund
Folio No: 1234567 / 89   PAN: ABCDE1234F   KYC: OK
HDFC Balanced Advantage Fund - Growth ISIN: INF179K01BE2 (Advisor: DIRECT) Registrar: CAMS
Opening Unit Balance: 0.000
Closing Unit Balance: 1,234.567 NAV on 30-Jun-2024: INR 45.6789
Total Cost Value: 50,000.00 Market Value on 30-Jun-2024: INR 56,393.12

Nippon India Mutual Fund
Folio No: 9988776655
Nippon India Gold Savings Fund - Growth ISIN: INF204KA1UB1 Registrar: KFINTECH
Closing Unit Balance: 2,000.000 NAV on 30-Jun-2024: INR 25.0000
Total Cost Value: 40,000.00 Market Value on 30-Jun-2024: INR 50,000.00

ICICI Prudential Mutual Fund
Folio No: 5551234
ICICI Prudential Silver ETF FoF - Growth ISIN: INF109KC1R14 Registrar: CAMS
Closing Unit Balance: 500.000 NAV on 30-Jun-2024: INR 12.5000
Total Cost Value: 5,000.00 Market Value on 30-Jun-2024: INR 6,250.00
"""


def test_find_statement_date_uses_valuation_date():
    assert _find_statement_date(_SAMPLE) == date(2024, 6, 30)


def test_parses_all_three_schemes():
    holdings = _parse_schemes(_SAMPLE)
    assert [h.name for h in holdings] == [
        "HDFC Balanced Advantage Fund - Growth",
        "Nippon India Gold Savings Fund - Growth",
        "ICICI Prudential Silver ETF FoF - Growth",
    ]


def test_units_nav_and_value_are_extracted():
    hdfc = _parse_schemes(_SAMPLE)[0]
    assert hdfc.isin == "INF179K01BE2"
    assert hdfc.units == pytest.approx(1234.567)
    assert hdfc.price == pytest.approx(45.6789)  # NAV, not the date's digits
    assert hdfc.value == pytest.approx(56393.12)  # market value, not cost value


def test_gold_and_silver_funds_are_reclassified():
    # A plain fund stays a mutual fund; gold/silver funds are routed out of MF.
    assert _by_isin(_SAMPLE, "INF179K01BE2").asset_class == "mutual_fund"
    assert _by_isin(_SAMPLE, "INF204KA1UB1").asset_class == "gold"
    assert _by_isin(_SAMPLE, "INF109KC1R14").asset_class == "silver"


def test_no_schemes_returns_empty():
    assert _parse_schemes("Just a cover page with no holdings.") == []


def _by_isin(text, isin):
    return next(h for h in _parse_schemes(text) if h.isin == isin)
