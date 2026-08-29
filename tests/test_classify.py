"""Tests for the asset-class rule engine.

Encodes the classification contract so tuning the keyword table later can't
silently regress the well-established rules (INF=MF, bond keywords beat the INE
equity default, section context wins, etc.).
"""

import pytest

from app.classify import AssetClass, Section, classify


def test_section_context_wins_for_nps_and_mf():
    assert classify(section=Section.NPS, isin="INE123456789") == AssetClass.NPS
    assert classify(section=Section.MUTUAL_FUND, isin="INE123456789") == AssetClass.MUTUAL_FUND


def test_inf_isin_is_mutual_fund():
    assert classify(isin="INF204K01234", description="Some Fund - Direct Growth") == AssetClass.MUTUAL_FUND


def test_ine_defaults_to_equity():
    assert classify(isin="INE002A01018", description="RELIANCE INDUSTRIES") == AssetClass.DIRECT_EQUITY


def test_bond_keyword_beats_ine_equity_default():
    # Corporate NCDs share the INE prefix with equity — description must win.
    assert classify(isin="INE001A07QW1", description="8.5% NCD SERIES II 2027") == AssetClass.DEBT
    assert classify(isin="INE123456789", description="XYZ LTD DEBENTURE") == AssetClass.DEBT


def test_sovereign_gold_bond_is_gold():
    assert classify(isin="IN0020190024", description="SGB 2.50% 2028 SR-II") == AssetClass.GOLD


def test_gold_fund_is_gold():
    assert classify(isin="INF204KA1UB1", description="Nippon India Gold Savings Fund") == AssetClass.GOLD


def test_silver_fund_is_silver():
    # Silver funds/ETFs must not fall through to the INF=mutual-fund default.
    assert classify(isin="INF109KC1R14", description="ICICI Prudential Silver ETF FoF") == AssetClass.SILVER


def test_govt_security_by_prefix_and_keyword():
    assert classify(isin="IN0020200070", description="7.26% GS 2033") == AssetClass.GOVT_SECURITY
    assert classify(isin="", description="91 DAY T-BILL") == AssetClass.GOVT_SECURITY


def test_etf_keyword():
    assert classify(isin="INE123456789", description="NIFTY BEES ETF") == AssetClass.ETF


def test_unknown_falls_through_to_other():
    assert classify(isin="XX999", description="mystery instrument") == AssetClass.OTHER


# --- AIF / VC / PE units ----------------------------------------------------
#
# SEBI mandated demat for AIF units, so angel/VC/PE commitments arrive in an NSDL
# CAS alongside the liquid holdings. Their ISINs are indistinguishable from the
# liquid stuff — INF like a mutual fund, INE like a share — so only the name
# separates them, and getting it wrong parks a decade-locked commitment in the
# Mutual Funds leaf looking redeemable.

@pytest.mark.parametrize("name", [
    "ABC INDIA GROWTH FUND AIF CATEGORY II",
    "XYZ ALTERNATIVE INVESTMENT FUND - CLASS A",
    "SOME ALTERNATE INVESTMENT FUND SCHEME I",
    "ABC VENTURE CAPITAL FUND - CLASS B",
    "BLUME VENTURES FUND IV UNITS",
    "KOTAK PRIVATE EQUITY FUND SERIES 3",
])
def test_aif_and_pe_units_are_not_mutual_funds(name):
    # The INF prefix would otherwise make every one of these a mutual fund.
    assert classify(section=Section.DEMAT, isin="INF1234567890",
                    description=name) is AssetClass.PRIVATE_EQUITY


def test_aif_with_a_corporate_isin_is_not_equity():
    """INE is an issuer prefix, so a VC fund can carry one — and would otherwise
    fall through to the direct-equity default."""
    assert classify(section=Section.DEMAT, isin="INE1234567890",
                    description="BLUME VENTURES FUND IV UNITS") is AssetClass.PRIVATE_EQUITY


@pytest.mark.parametrize("name", [
    "HDFC FLEXI CAP FUND - DIRECT GROWTH",
    "SBI SMALL CAP FUND REGULAR GROWTH",
    "ICICI PRU VALUE DISCOVERY FUND",
    "UTI NIFTY 50 INDEX FUND",
    "PARAG PARIKH FLEXI CAP FUND",
])
def test_ordinary_mutual_funds_still_classify_as_mutual_funds(name):
    """The AIF rules must not swallow the liquid funds they sit next to."""
    assert classify(section=Section.DEMAT, isin="INF1234567890",
                    description=name) is AssetClass.MUTUAL_FUND


# --- The signal that actually works: the statement says so -------------------
#
# Name patterns were always a guess. NSDL writes the thing that genuinely makes
# these holdings different into the security name itself:
#
#   "AL Trust - Slang Labs 2-Category I-Slang Labs 2 Close ended -
#    Restricted Transferability"
#
# AIF and PE units are non-transferable and no mutual fund is, so this beats
# every convention-matching rule — it's structural rather than editorial.

REAL_AIF_NAMES = [
    "AL Trust - Slang Labs 2-Category I-Slang Labs 2 Close ended - Restricted Transferability",
    "AL Trust - Teachmint 2-Category I-Teachmint Close ended - Restricted Transferability",
]


@pytest.mark.parametrize("name", REAL_AIF_NAMES)
def test_real_aif_names_from_a_statement(name):
    """These carry an INF prefix, so without a name rule they read as mutual funds."""
    assert classify(section=Section.DEMAT, isin="INF1234567890",
                    description=name) is AssetClass.PRIVATE_EQUITY


def test_restricted_transferability_alone_is_enough():
    assert classify(section=Section.DEMAT, isin="INF1234567890",
                    description="Some Fund - Restricted Transferability"
                    ) is AssetClass.PRIVATE_EQUITY


def test_sebi_category_does_not_need_the_word_fund_nearby():
    """The first version of this rule required "fund" next to "Category", which
    is exactly why the real names above slipped through — they have no "fund"."""
    assert classify(section=Section.DEMAT, isin="INF1234567890",
                    description="AL Trust - Teachmint 2-Category I-Teachmint"
                    ) is AssetClass.PRIVATE_EQUITY


@pytest.mark.parametrize("name", [
    "NIPPON INDIA FIXED MATURITY PLAN SERIES 44",   # genuinely close-ended, but an MF
    "ADITYA BIRLA SUN LIFE FRONTLINE EQUITY FUND",
    "ICICI PRU VALUE DISCOVERY FUND",
    "UTI NIFTY 50 INDEX FUND",
])
def test_the_new_rules_do_not_swallow_ordinary_funds(name):
    """Close-ended mutual funds exist, so "close ended" is deliberately *not* a
    signal on its own — only restricted transferability and the SEBI category."""
    assert classify(section=Section.DEMAT, isin="INF1234567890",
                    description=name) is AssetClass.MUTUAL_FUND
