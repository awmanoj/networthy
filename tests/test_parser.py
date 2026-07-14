"""Tests for the CAS text-extraction helpers.

These exercise the field-extraction logic against representative text snippets
(the fragile part), without needing a real password-protected PDF.
"""

from datetime import date

import pytest

from app.parser.nsdl_cas import (
    CASParseError,
    _find_accounts,
    _find_statement_date,
    _find_total_value,
    _parse_holding_line,
    _to_float,
)
from app.classify import Section


def test_to_float_strips_indian_grouping():
    assert _to_float("12,34,567.89") == pytest.approx(1234567.89)
    assert _to_float("1,000") == 1000.0
    assert _to_float("45000.50") == pytest.approx(45000.50)
    assert _to_float("not-a-number") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Consolidated Account Statement as on 30-Jun-2024", date(2024, 6, 30)),
        ("... as on 30-JUN-2024 ...", date(2024, 6, 30)),
        ("Statement as on 31/03/2023", date(2023, 3, 31)),
    ],
)
def test_find_statement_date(text, expected):
    assert _find_statement_date(text) == expected


def test_find_statement_date_missing_raises():
    with pytest.raises(CASParseError):
        _find_statement_date("no date anywhere here")


def test_find_total_value_consolidated_portfolio():
    text = "Consolidated Portfolio Value       12,34,567.89"
    assert _find_total_value(text) == pytest.approx(1234567.89)


def test_find_total_value_grand_total():
    text = "Grand Total : 9,87,654.00"
    assert _find_total_value(text) == pytest.approx(987654.00)


def test_find_total_value_missing_raises():
    with pytest.raises(CASParseError):
        _find_total_value("nothing that looks like a portfolio total")


# --- Detailed holding extraction --------------------------------------------

def test_parse_holding_line_isin_leads_row():
    """'<ISIN> <name> <balance> <price> <value>' — the demat row shape."""
    h = _parse_holding_line(
        "INE009A01021 INFOSYS LIMITED 100 1,500.00 1,50,000.00", Section.DEMAT
    )
    assert h is not None
    assert h.isin == "INE009A01021"
    assert h.name == "INFOSYS LIMITED"  # numeric tail must not bleed into the name
    assert h.asset_class == "direct_equity"
    assert h.units == pytest.approx(100.0)
    assert h.price == pytest.approx(1500.0)
    assert h.value == pytest.approx(150000.0)


def test_parse_holding_line_name_leads_row():
    """'<name> <ISIN> <units> <nav> <value>' — the other common shape."""
    h = _parse_holding_line(
        "HDFC Balanced Advantage Fund INF179K01BE2 500.123 45.67 22,842.11",
        Section.MUTUAL_FUND,
    )
    assert h.name == "HDFC Balanced Advantage Fund"
    assert h.isin == "INF179K01BE2"
    assert h.asset_class == "mutual_fund"
    assert h.units == pytest.approx(500.123)
    assert h.value == pytest.approx(22842.11)


def test_parse_holding_line_ncd_beats_ine_equity_default():
    """A bond keyword must win over the INE=equity ISIN fallback."""
    h = _parse_holding_line("INE123A07011 TATA CAPITAL NCD 10 1,00,000.00", Section.DEMAT)
    assert h.asset_class == "debt"


def test_parse_holding_line_no_isin_is_skipped():
    assert _parse_holding_line("Opening Balance carried forward", Section.DEMAT) is None


_SAMPLE_CAS = """\
Consolidated Account Statement as on 30-Jun-2024
Consolidated Portfolio Value 12,34,567.89

National Securities Depository Limited (NSDL)
DP Name : ZERODHA BROKING LIMITED
DP ID : 12081600  Client ID : 12345678
ISIN Security Current Bal Market Price Value
INE009A01021 INFOSYS LIMITED 100 1,500.00 1,50,000.00
INE040A01034 HDFC BANK LIMITED 50 1,600.50 80,025.00

Mutual Fund Folios
HDFC MUTUAL FUND
Folio No : 1234567/89
INF179K01BE2 HDFC Balanced Advantage Fund 500.123 45.67 22,842.11
"""


def test_find_accounts_groups_by_account():
    accounts = _find_accounts(_SAMPLE_CAS)
    assert len(accounts) == 2

    demat, mf = accounts
    assert demat.kind == "demat"
    assert demat.depository == "NSDL"
    assert demat.name == "ZERODHA BROKING LIMITED"
    assert demat.identifier == "12081600 / 12345678"
    assert [h.name for h in demat.holdings] == ["INFOSYS LIMITED", "HDFC BANK LIMITED"]
    assert all(h.asset_class == "direct_equity" for h in demat.holdings)

    assert mf.kind == "mutual_fund"
    assert mf.name == "HDFC MUTUAL FUND"
    assert mf.identifier == "1234567/89"
    assert mf.holdings[0].asset_class == "mutual_fund"
    assert mf.value == pytest.approx(22842.11)


def test_find_accounts_empty_text_yields_nothing():
    assert _find_accounts("no holdings here, just prose") == []
