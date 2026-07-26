"""Structure of the manual net-worth breakdown (the /networth pages).

A declarative tree drives both the overview and the per-node pages, so the whole
Assets / Liabilities hierarchy lives in one place. Leaf pages are intentionally
blank for now — this tree is the scaffold we fill in later. To add a category,
add a `Node`; the routes and templates pick it up automatically.
"""

from __future__ import annotations

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
            Node("fixed-income", "Fixed Income",
                 note="Corporate bonds, govt bonds, PPF, SSA, NSC"),
            Node("bank-cash", "Bank Account & Cash"),
            Node("foreign-exchange", "Foreign Exchange"),
            Node("alternate-investments", "Alternate Investments",
                 note="Startups / angel investments, ESOPs in companies"),
        ]),
        Node("non-financial-assets", "Non-Financial Assets", children=[]),
    ]),
    Node("liabilities", "Liabilities", children=[]),
]

ROOT = Node("", "Networth", children=SECTIONS)


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
    crumbs = [("Networth", "/networth")]
    parts: list[str] = []
    for node in chain:
        parts.append(node.slug)
        crumbs.append((node.title, "/networth/" + "/".join(parts)))
    return crumbs
