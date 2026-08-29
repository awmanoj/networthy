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
from app.classify import AssetClass, Section


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


def test_parse_holding_line_without_amounts_is_skipped():
    # Interim hardening: an ISIN line carrying no amount (values wrapped to the
    # next text line, or a bare "ISIN :" label) is dropped, not emitted blank.
    assert _parse_holding_line(
        "INF179K01BE2 HDFC Balanced Advantage Fund - Growth", Section.MUTUAL_FUND
    ) is None
    assert _parse_holding_line("ISIN : INE009A01021", Section.DEMAT) is None


def test_parse_holding_line_keeps_digits_in_name():
    # "E2E" must survive — the value columns are the trailing numbers, so a digit
    # inside the name isn't mistaken for the start of the numbers (regression: the
    # name used to collapse to "E").
    h = _parse_holding_line(
        "INE255Z01027 E2E NETWORKS LIMITED 1.00 8,000 396.30 31,70,400.00", Section.DEMAT
    )
    assert h.name == "E2E NETWORKS LIMITED"
    assert h.asset_class == "direct_equity"
    assert h.units == pytest.approx(8000.0)   # face value 1.00 is ignored
    assert h.price == pytest.approx(396.30)
    assert h.value == pytest.approx(3170400.0)


def test_parse_holding_line_prose_isin_mention_is_skipped():
    # A line that merely names an ISIN (no trailing numeric columns) is not a holding,
    # even though the name carries a digit ("E2E").
    assert _parse_holding_line(
        "ISIN : INE255Z01027 - E2E NETWORKS LIMITED", Section.DEMAT
    ) is None


def test_parse_holding_line_nil_zero_value_row_is_skipped():
    # A deleted/nil holding (0 units, "See Note", value 0.00) adds nothing.
    assert _parse_holding_line(
        "INE255Z01019 E2E NETWORKS LIMITED 10.00 0 See Note 0.00", Section.DEMAT
    ) is None


def test_find_accounts_captures_ticker_under_equity_row():
    # The exchange ticker printed on its own line under an equity row (the key we
    # use to fetch a live price) is stitched onto that holding, not treated as a
    # separate holding or a name tail.
    text = (
        "National Securities Depository Limited (NSDL)\n"
        "INE255Z01027 E2E NETWORKS LIMITED 1.00 8,000 396.30 31,70,400.00\n"
        "E2E.NSE\n"
    )
    accounts = _find_accounts(text)
    holdings = [h for a in accounts for h in a.holdings]
    assert len(holdings) == 1
    assert holdings[0].name == "E2E NETWORKS LIMITED"   # ticker not appended to name
    assert holdings[0].ticker == "E2E.NSE"


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


_WRAP_CAS = """\
National Securities Depository Limited (NSDL)
DP Name : SOME BROKER
INE255Z01027 E2E NETWORKS LIMITED 1.00 8,000 396.30 31,70,400.00
E2E.NSE
INF204K01562 NIPPON INDIA LARGE CAP FUND GROWTH 1,041.194 89.39 93,070.45
PLAN GROWTH OPTION
"""


def test_find_accounts_stitches_wrapped_names_but_skips_tickers():
    names = [h.name for a in _find_accounts(_WRAP_CAS) for h in a.holdings]
    # The ticker line ("E2E.NSE") is NOT appended to the stock name…
    assert names[0] == "E2E NETWORKS LIMITED"
    # …but a wrapped scheme name spilling onto the next line is stitched back on.
    assert names[1] == "NIPPON INDIA LARGE CAP FUND GROWTH PLAN GROWTH OPTION"


# --- AIF rows: face value written into the security name ---------------------
#
# NSDL puts an AIF's face value inside the name — "…-FACE VALUE INR 100.0/- AND
# PAID UP" — so the name carries digits immediately before the real columns. An
# unlisted AIF has no market price, so its row has only two of them, and the
# positional read would otherwise take the face value as units and shift
# everything along.

_AIF_ROWS = [
    "AL TRUST NUCLEON HEALTH CATEGORY-I-CLASS NUCLEON HEALTH 2 FACE VALUE INR 100.0/- AND PAID UP",
    "AL TRUST BASIL-CATEGORY-I-CLASS BASIL -FACE VALUE INR 100.0/- AND PAID UP",
    "AL TRUST KIVI AGROSPERITY-CATEGORY-I CLASS KIVI AGROSPERITY -FACE VALUE INR 100.0/- AND PAID UP VALUE INR 100.0/",
    "LV FARMDIDI PI-CATEGORY-I-CLASS A FACE VALUE INR 100000.0/- AND PAID UP",
    "INFINYTE ALLINCAPITAL-CATEGORY-I CLASS A -SR 0109-FACE VALUE INR 1000.0/ AND PAID UP VALUE INR 1000.0/- DATE OF",
]


@pytest.mark.parametrize("name", _AIF_ROWS)
def test_aif_row_with_two_columns_reads_units_not_face_value(name):
    """units + value only, as an unlisted holding has."""
    line = f"INE002A08534 {name} 1000.000 100000.00"
    h = _parse_holding_line(line, Section.DEMAT)
    assert h is not None
    assert h.units == 1000.0, "face value was read as units"
    assert h.price is None
    assert h.value == 100000.0
    assert h.asset_class == AssetClass.PRIVATE_EQUITY.value


@pytest.mark.parametrize("name", _AIF_ROWS)
def test_aif_row_with_three_columns(name):
    line = f"INE002A08534 {name} 500.000 200.0000 100000.00"
    h = _parse_holding_line(line, Section.DEMAT)
    assert (h.units, h.price, h.value) == (500.0, 200.0, 100000.0)


@pytest.mark.parametrize("name", _AIF_ROWS)
def test_the_name_survives_masking_intact(name):
    """The face value is hidden from the *column scan* only — the name is sliced
    from the original text and must still read as the statement wrote it."""
    line = f"INE002A08534 {name} 1000.000 100000.00"
    h = _parse_holding_line(line, Section.DEMAT)
    assert h.name == name


def test_ordinary_rows_are_unaffected_by_the_masking():
    h = _parse_holding_line(
        "INE002A08534 HDFC LTD EQUITY SHARES 100.000 2500.0000 250000.00", Section.DEMAT)
    assert (h.units, h.price, h.value) == (100.0, 2500.0, 250000.0)


# --- Folio numbers are not amounts -------------------------------------------
#
# A flattened CAS line can carry a folio or account number next to the holding.
# Money in a statement is always written with decimals or Indian grouping, so a
# long bare digit run is an identifier. Reading one as a value produced a real
# ₹477,280,532,916 "holding" with no units and no price.

def test_a_folio_number_is_not_read_as_a_value():
    """The row that shipped: name, ISIN, then a bare 12-digit folio number."""
    line = "ISIN ETF FUND OF FUND (FOF) - GROWTH PLAN INF204KC1345 477280532916"
    assert _parse_holding_line(line, Section.MUTUAL_FUND) is None


def test_a_folio_number_beside_real_columns_is_skipped():
    line = "HDFC FLEXI CAP FUND INF179K01158 477280532916 1000.000 12.3456 15234.56"
    h = _parse_holding_line(line, Section.MUTUAL_FUND)
    assert (h.units, h.price, h.value) == (1000.0, 12.3456, 15234.56)
    assert "477280532916" not in h.name


@pytest.mark.parametrize("token,expected", [
    ("12345678", 12345678.0),        # 8 digits — could be an amount, kept
    ("1,23,45,678", 12345678.0),     # grouped — unambiguously an amount
    ("12345678.90", 12345678.90),    # decimals — unambiguously an amount
])
def test_plausible_amounts_survive(token, expected):
    h = _parse_holding_line(f"X FUND INF179K01158 {token}", Section.MUTUAL_FUND)
    assert h is not None and h.value == expected


@pytest.mark.parametrize("token", ["123456789", "477280532916", "91234567890123"])
def test_long_bare_digit_runs_are_treated_as_identifiers(token):
    assert _parse_holding_line(f"X FUND INF179K01158 {token}",
                               Section.MUTUAL_FUND) is None


def test_grouped_crore_values_are_still_amounts():
    """The guard keys on formatting, not magnitude — a genuinely large holding
    written the way a statement writes it must survive."""
    h = _parse_holding_line(
        "BIG FUND INF179K01158 1000.000 5000.0000 50,00,00,000.00", Section.MUTUAL_FUND)
    assert h.value == 500000000.0


# --- Cleaning up rows already stored with an identifier as their value --------

def test_drop_phantom_holdings_removes_only_the_bad_shape(tmp_path, monkeypatch):
    """The parser fix reaches new uploads only, and one of these rows adds
    hundreds of crore to a net worth — too wrong to leave until a re-upload."""
    from datetime import date as _date
    from app import storage
    from app.models import Account, Holding, Snapshot

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    uid = storage.get_or_create_user("phantom@test.com").id
    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=_date(2026, 6, 30), total_value=0.0,
        holding_count=4, source_filename="cas.pdf"))
    storage.replace_holdings(sid, [Account(kind="mutual_fund", name="MF", holdings=[
        # The real row: no units, no price, a folio number as the value.
        Holding("ISIN ETF FUND OF FUND (FOF)", "mutual_fund", "INF204KC1345",
                None, None, 477_280_532_916.0),
        # Large but genuine — has units, so untouched however big it is.
        Holding("Big Real Holding", "mutual_fund", "INF1", 5000.0, 40000.0, 200_000_000.0),
        # No units/price but a sane value — untouched.
        Holding("Small Unpriced", "mutual_fund", "INF2", None, None, 50_000.0),
        Holding("Ordinary", "mutual_fund", "INF3", 100.0, 10.0, 1_000.0),
    ])])

    assert storage.drop_phantom_holdings() == 1
    names = {h["name"] for h in storage.latest_holdings_by_class(uid, {"mutual_fund"})}
    assert names == {"Big Real Holding", "Small Unpriced", "Ordinary"}
    assert storage.drop_phantom_holdings() == 0          # idempotent
