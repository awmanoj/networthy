"""Tests for the CAS text-extraction helpers.

These exercise the field-extraction logic against representative text snippets
(the fragile part), without needing a real password-protected PDF.
"""

from datetime import date

import pytest

from app.parser.nsdl_cas import (
    CASParseError,
    _find_statement_date,
    _find_total_value,
    _to_float,
)


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
