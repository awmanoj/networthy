"""Tests for the asset-class rule engine.

Encodes the classification contract so tuning the keyword table later can't
silently regress the well-established rules (INF=MF, bond keywords beat the INE
equity default, section context wins, etc.).
"""

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


def test_govt_security_by_prefix_and_keyword():
    assert classify(isin="IN0020200070", description="7.26% GS 2033") == AssetClass.GOVT_SECURITY
    assert classify(isin="", description="91 DAY T-BILL") == AssetClass.GOVT_SECURITY


def test_etf_keyword():
    assert classify(isin="INE123456789", description="NIFTY BEES ETF") == AssetClass.ETF


def test_unknown_falls_through_to_other():
    assert classify(isin="XX999", description="mystery instrument") == AssetClass.OTHER
