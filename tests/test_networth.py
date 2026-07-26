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


def test_financial_asset_leaves_in_order():
    fin = resolve("assets/financial-assets")[-1]
    assert [c.slug for c in fin.children] == [
        "mutual-funds", "equity", "fixed-income",
        "bank-cash", "foreign-exchange", "alternate-investments",
    ]
    assert all(c.is_leaf for c in fin.children)  # leaves are blank for now


def test_non_financial_and_liabilities_are_blank():
    assert resolve("assets/non-financial-assets")[-1].children == []
    assert resolve("liabilities")[-1].children == []


def test_resolve_root_leaf_and_unknown():
    assert resolve("") == []  # root
    assert resolve("assets/financial-assets/mutual-funds")[-1].title == "Mutual Funds"
    assert resolve("assets/nope") is None
    assert resolve("assets/financial-assets/bogus") is None


def test_breadcrumbs_build_cumulative_urls():
    crumbs = breadcrumbs(resolve("assets/financial-assets/equity"))
    assert [t for t, _ in crumbs] == ["Net worth", "Assets", "Financial Assets", "Equity"]
    assert crumbs[0] == ("Net worth", "/networth")
    assert crumbs[-1] == ("Equity", "/networth/assets/financial-assets/equity")
