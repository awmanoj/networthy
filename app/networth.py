"""Structure of the manual net-worth breakdown (the /networth pages).

A declarative tree drives both the overview and the per-node pages, so the whole
Assets / Liabilities hierarchy lives in one place. Leaf pages are intentionally
blank for now — this tree is the scaffold we fill in later. To add a category,
add a `Node`; the routes and templates pick it up automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Node:
    """One category in the net-worth tree. A node with no children is a leaf."""

    slug: str          # URL segment (kebab-case)
    title: str         # display name
    note: str = ""     # optional one-line description
    children: list["Node"] = field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        return not self.children


# Top-level sections (the root, "Networth", is implicit at /networth).
SECTIONS: list[Node] = [
    Node("assets", "Assets", children=[
        Node("financial-assets", "Financial Assets", children=[
            Node("mutual-funds", "Mutual Funds"),
            Node("equity", "Equity", note="Direct stocks — IN"),
            Node("foreign-equity", "Foreign / US Equity",
                 note="US & international stocks"),
            Node("crypto", "Crypto", note="Bitcoin, Ethereum & other coins"),
            Node("fixed-income", "Fixed Income",
                 note="Bonds, FDs/RDs, PPF, EPF, NPS, SSA, NSC", children=[
                Node("corporate-bonds", "Corporate Bonds"),
                Node("govt-bonds", "Government Bonds"),
                Node("fixed-deposits", "Fixed Deposits / RD"),
                Node("ppf", "PPF", note="Public Provident Fund"),
                Node("epf", "EPF / PF", note="Employees' Provident Fund"),
                Node("nps", "NPS", note="National Pension System"),
                Node("sukanya-samriddhi", "Sukanya Samriddhi (SSA)"),
                Node("nsc", "NSC", note="National Savings Certificate"),
                Node("other-fixed-income", "Other Fixed Income",
                     note="Anything else fixed-income"),
            ]),
            Node("gold-silver", "Gold & Silver",
                 note="SGBs, Gold/Silver ETFs & funds, digital gold"),
            Node("bank-cash", "Bank Account & Cash", children=[
                Node("bank-accounts", "Bank Accounts",
                     note="Savings & current account balances"),
                Node("cash", "Cash", note="Cash in hand"),
            ]),
            Node("foreign-exchange", "Foreign Exchange"),
            Node("alternate-investments", "Alternate Investments",
                 note="Startups / angel investments, ESOPs in companies"),
            Node("others", "Others", note="Anything that doesn't fit above"),
        ]),
        Node("non-financial-assets", "Non-Financial Assets", children=[
            Node("real-estate", "Real Estate",
                 note="Home, other property, commercial, land, under-construction",
                 children=[
                Node("primary-residence", "Primary Residence",
                     note="Self-occupied home"),
                Node("residential-property", "Other Residential Property",
                     note="Second home, rented flats"),
                Node("commercial-property", "Commercial Property",
                     note="Shops, offices"),
                Node("land", "Land / Plots", note="Residential or agricultural"),
                Node("under-construction", "Under-construction Property",
                     note="Amount paid to builder"),
            ]),
            Node("physical-gold", "Physical Gold & Jewellery",
                 note="Coins, bars, ornaments"),
            Node("private-business", "Private Business",
                 note="Unlisted / operating business ownership"),
        ]),
    ]),
    Node("liabilities", "Liabilities", children=[
        Node("secured-loans", "Secured Loans", children=[
            Node("home-loan", "Home Loan"),
            Node("loan-against-property", "Loan Against Property"),
            Node("vehicle-loan", "Vehicle Loan"),
            Node("loan-against-securities", "Loan Against Securities",
                 note="Against shares / MFs"),
            Node("gold-loan", "Gold Loan"),
        ]),
        Node("unsecured-loans", "Unsecured Loans", children=[
            Node("personal-loan", "Personal Loan"),
            Node("credit-card", "Credit Card Outstanding",
                 note="Carried / overdue balance only"),
            Node("education-loan", "Education Loan"),
            Node("consumer-emi", "Consumer / BNPL EMIs"),
        ]),
    ]),
]

ROOT = Node("", "Networth", children=SECTIONS)


# Leaf slug -> the asset classes whose holdings that leaf page displays. Only leaves
# listed here are "data-backed" (render a holdings table instead of a blank state);
# the rest stay blank scaffolds for now. Keep slugs in sync with the tree above.
LEAF_ASSET_CLASSES: dict[str, set[str]] = {
    "mutual-funds": {"mutual_fund"},
    "gold-silver": {"gold", "silver"},
    "equity": {"direct_equity"},
    # Bonds & NPS held in demat are already classified in the NSDL CAS, so these
    # leaves auto-fill from it (and also accept manual entries — see MANUAL_LEAVES).
    "corporate-bonds": {"debt"},
    "govt-bonds": {"govt_security"},
    "nps": {"nps"},
}

# Where each data-backed leaf's holdings come from — drives the page's call-to-action.
# "cams" -> import a CAMS statement; "nsdl" -> upload an NSDL CAS. (Direct equity only
# exists in the depository CAS, never in a CAMS mutual-fund statement.)
LEAF_IMPORT: dict[str, str] = {
    "mutual-funds": "cams",
    "gold-silver": "cams",
    "equity": "nsdl",
    "corporate-bonds": "nsdl",
    "govt-bonds": "nsdl",
    "nps": "nsdl",
}

# Leaves that accept hand-entered holdings (Scheme / Investment / Maturity / Years /
# Rate). Some (bonds, NPS) also auto-fill from the CAS — manual supplements those;
# the rest (PPF, EPF, SSA, NSC, Others) exist only as manual entries.
MANUAL_LEAVES: set[str] = {
    "corporate-bonds", "govt-bonds", "nps",
    "ppf", "epf", "sukanya-samriddhi", "nsc",
    "fixed-deposits", "other-fixed-income", "others",
}

# Leaves backed by hand-entered bank-account / cash balances (their own shape:
# bank name, type, nickname, balance — handled specially in the web layer).
BANK_CASH_LEAVES: set[str] = {"bank-accounts", "cash"}

# The five Real Estate sub-leaves — each holds hand-entered properties (label,
# current value, cost, purchase date, notes), handled specially in the web layer.
REALTY_LEAVES: set[str] = {
    "primary-residence", "residential-property", "commercial-property",
    "land", "under-construction",
}

# Every Liabilities leaf — each holds hand-entered loans/dues (lender, outstanding
# balance, and optional principal/rate/EMI/end-date). Outstanding is what net worth
# subtracts.
LIABILITY_LEAVES: set[str] = {
    "home-loan", "loan-against-property", "vehicle-loan",
    "loan-against-securities", "gold-loan",
    "personal-loan", "credit-card", "education-loan", "consumer-emi",
}


def rollup(leaf_value: Callable[[str], float | None]) -> dict[str, float]:
    """Total value per node, keyed by its '/'-joined slug path.

    `leaf_value(slug)` returns a leaf's value, or None if that leaf isn't
    data-backed. A parent's value is the sum of its descendants'. Only nodes that
    end up with a non-zero value appear in the result, so the templates can show a
    number where there's data and stay quiet elsewhere.
    """
    values: dict[str, float] = {}

    def visit(node: Node, parts: list[str]) -> float:
        path = "/".join(parts)
        if node.is_leaf:
            v = leaf_value(node.slug) or 0.0
            if v:
                values[path] = v
            return v
        total = sum(visit(c, parts + [c.slug]) for c in node.children)
        if total:
            values[path] = total
        return total

    for section in SECTIONS:
        visit(section, [section.slug])
    return values


def resolve(path: str) -> list[Node] | None:
    """Resolve a '/'-joined slug path to the node chain from a top section down.

    Returns ``[]`` for the empty (root) path, the chain of nodes ending at the
    target for a valid path, or ``None`` if any slug doesn't match. The chain
    excludes ROOT.
    """
    chain: list[Node] = []
    current = ROOT
    for part in (p for p in path.split("/") if p):
        match = next((c for c in current.children if c.slug == part), None)
        if match is None:
            return None
        chain.append(match)
        current = match
    return chain


def breadcrumbs(chain: list[Node]) -> list[tuple[str, str]]:
    """(title, url) pairs from the root down to and including the current node."""
    crumbs = [("Net worth", "/networth")]
    parts: list[str] = []
    for node in chain:
        parts.append(node.slug)
        crumbs.append((node.title, "/networth/" + "/".join(parts)))
    return crumbs
