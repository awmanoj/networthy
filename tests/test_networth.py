"""Tests for the net-worth breakdown tree.

Pins the scaffold structure the page was specified with, so future edits to the
tree can't silently drop a section or break path resolution.
"""

from app.networth import SECTIONS, breadcrumbs, resolve


def test_top_level_sections():
    assert [s.slug for s in SECTIONS] == ["assets", "liabilities"]


def test_assets_split_into_financial_and_non_financial():
    assets = resolve("assets")[-1]
    assert [c.slug for c in assets.children] == [
        "financial-assets", "non-financial-assets"
    ]


def test_financial_assets_children_in_order():
    fin = resolve("assets/financial-assets")[-1]
    assert [c.slug for c in fin.children] == [
        "mutual-funds", "equity", "foreign-equity", "fixed-income",
        "gold", "bank-cash", "foreign-exchange", "alternate-investments",
    ]


def test_fixed_income_has_instrument_children():
    fi = resolve("assets/financial-assets/fixed-income")[-1]
    assert not fi.is_leaf  # now a drill-in category, not a blank leaf
    assert [c.slug for c in fi.children] == [
        "corporate-bonds", "govt-bonds", "fixed-deposits", "ppf",
        "epf", "nps", "sukanya-samriddhi", "nsc",
    ]
    assert all(c.is_leaf for c in fi.children)


def test_non_financial_assets_children_in_order():
    nfa = resolve("assets/non-financial-assets")[-1]
    assert [c.slug for c in nfa.children] == [
        "real-estate", "physical-gold", "private-business",
    ]


def test_real_estate_sub_types():
    real_estate = resolve("assets/non-financial-assets/real-estate")[-1]
    assert [c.slug for c in real_estate.children] == [
        "primary-residence", "residential-property", "commercial-property",
        "land", "under-construction",
    ]
    assert all(c.is_leaf for c in real_estate.children)


def test_liabilities_split_into_secured_and_unsecured():
    liab = resolve("liabilities")[-1]
    assert [c.slug for c in liab.children] == ["secured-loans", "unsecured-loans"]


def test_secured_loan_leaves():
    secured = resolve("liabilities/secured-loans")[-1]
    assert [c.slug for c in secured.children] == [
        "home-loan", "loan-against-property", "vehicle-loan",
        "loan-against-securities", "gold-loan",
    ]
    assert all(c.is_leaf for c in secured.children)


def test_unsecured_loan_leaves():
    unsecured = resolve("liabilities/unsecured-loans")[-1]
    assert [c.slug for c in unsecured.children] == [
        "personal-loan", "credit-card", "education-loan", "consumer-emi",
    ]
    assert all(c.is_leaf for c in unsecured.children)


def test_resolve_root_leaf_and_unknown():
    assert resolve("") == []  # root
    assert resolve("assets/financial-assets/mutual-funds")[-1].title == "Mutual Funds"
    # Fourth-level leaves resolve too.
    assert resolve("assets/financial-assets/fixed-income/epf")[-1].title == "EPF / PF"
    assert resolve("assets/non-financial-assets/real-estate/land")[-1].title == "Land / Plots"
    assert resolve("assets/nope") is None
    assert resolve("assets/financial-assets/fixed-income/bogus") is None


def test_breadcrumbs_build_cumulative_urls():
    crumbs = breadcrumbs(resolve("assets/financial-assets/equity"))
    assert [t for t, _ in crumbs] == ["Networth", "Assets", "Financial Assets", "Equity"]
    assert crumbs[0] == ("Networth", "/networth")
    assert crumbs[-1] == ("Equity", "/networth/assets/financial-assets/equity")
