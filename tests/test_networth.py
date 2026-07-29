"""Tests for the net-worth breakdown tree.

Pins the scaffold structure the page was specified with, so future edits to the
tree can't silently drop a section or break path resolution.
"""

from app.networth import (
    LEAF_ASSET_CLASSES,
    LEAF_IMPORT,
    SECTIONS,
    breadcrumbs,
    resolve,
    rollup,
)


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
        "gold-silver", "bank-cash", "foreign-exchange", "alternate-investments",
        "others",
    ]


def test_fixed_income_has_instrument_children():
    fi = resolve("assets/financial-assets/fixed-income")[-1]
    assert not fi.is_leaf  # now a drill-in category, not a blank leaf
    assert [c.slug for c in fi.children] == [
        "corporate-bonds", "govt-bonds", "fixed-deposits", "ppf",
        "epf", "nps", "sukanya-samriddhi", "nsc", "other-fixed-income",
    ]
    assert all(c.is_leaf for c in fi.children)


def test_bank_cash_split_into_bank_and_cash_leaves():
    bc = resolve("assets/financial-assets/bank-cash")[-1]
    assert not bc.is_leaf  # a drill-in category now
    assert [c.slug for c in bc.children] == ["bank-accounts", "cash"]
    assert all(c.is_leaf for c in bc.children)
    from app.networth import BANK_CASH_LEAVES
    assert BANK_CASH_LEAVES == {"bank-accounts", "cash"}


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


def test_data_backed_leaves_map_to_asset_classes():
    # The data-backed leaves must exist in the tree with these exact slugs.
    assert LEAF_ASSET_CLASSES == {
        "mutual-funds": {"mutual_fund"},
        "gold-silver": {"gold", "silver"},
        "equity": {"direct_equity"},
        "corporate-bonds": {"debt"},
        "govt-bonds": {"govt_security"},
        "nps": {"nps"},
    }
    # Every data-backed leaf declares where its holdings come from.
    assert set(LEAF_IMPORT) == set(LEAF_ASSET_CLASSES)
    assert LEAF_IMPORT["equity"] == "nsdl"


def test_manual_leaves_resolve_and_bonds_overlap_cas():
    from app.networth import MANUAL_LEAVES
    # Corporate bonds / govt bonds / NPS are BOTH CAS-backed and manual-enabled.
    for slug in ("corporate-bonds", "govt-bonds", "nps"):
        assert slug in LEAF_ASSET_CLASSES and slug in MANUAL_LEAVES
    # Purely-manual leaves have no CAS class.
    for slug in ("ppf", "epf", "sukanya-samriddhi", "nsc", "others", "other-fixed-income"):
        assert slug in MANUAL_LEAVES and slug not in LEAF_ASSET_CLASSES


def test_rollup_sums_leaves_into_parents():
    leaf_values = {"mutual-funds": 100.0, "equity": 200.0, "gold-silver": 50.0}
    out = rollup(lambda slug: leaf_values.get(slug))

    assert out["assets/financial-assets/mutual-funds"] == 100.0
    assert out["assets/financial-assets/equity"] == 200.0
    assert out["assets/financial-assets/gold-silver"] == 50.0
    assert out["assets/financial-assets"] == 350.0   # parent sums its descendants
    assert out["assets"] == 350.0


def test_rollup_omits_nodes_without_data():
    out = rollup(lambda slug: {"equity": 200.0}.get(slug))
    assert out["assets/financial-assets/equity"] == 200.0
    assert out["assets"] == 200.0
    assert "liabilities" not in out                         # no data under liabilities
    assert "assets/financial-assets/mutual-funds" not in out  # empty leaf not keyed
    assert "assets/non-financial-assets" not in out


def test_rollup_empty_when_no_leaf_has_value():
    assert rollup(lambda slug: None) == {}


def test_breadcrumbs_build_cumulative_urls():
    crumbs = breadcrumbs(resolve("assets/financial-assets/equity"))
    # Root is the Dashboard (the tree overview lives on the home page now).
    assert [t for t, _ in crumbs] == ["Dashboard", "Assets", "Financial Assets", "Equity"]
    assert crumbs[0] == ("Dashboard", "/")
    assert crumbs[-1] == ("Equity", "/networth/assets/financial-assets/equity")
