"""Asset-class classification for CAS holdings.

CAS layouts vary across issuers and periods, so classification is a layered,
config-driven rule engine rather than hardcoded logic. Signals, strongest first:

  1. Section context   — which CAS section a row came from (most reliable anchor)
  2. ISIN prefix       — INF=mutual fund, IN0x=govt security, INE=corporate
  3. Description keywords — separates corporate equity vs bond/NCD, gold, ETF
  4. Manual override   — set in the UI; always wins (handled by the caller)

When nothing matches we return OTHER and let the caller flag the row for review,
rather than guessing silently.

Note the deliberate trap this guards against: corporate **bonds/NCDs share the
`INE` prefix with equity**, so ISIN alone cannot separate them — the description
keywords in step 3 are what disambiguate.
"""

from __future__ import annotations

import re
from enum import Enum


class AssetClass(str, Enum):
    # --- Sourced from the CAS ---
    DIRECT_EQUITY = "direct_equity"
    MUTUAL_FUND = "mutual_fund"
    DEBT = "debt"  # bonds, NCDs, debentures
    GOVT_SECURITY = "govt_security"  # G-Sec, T-Bill
    GOLD = "gold"  # SGB, gold ETF/fund
    ETF = "etf"
    NPS = "nps"
    # --- Manual entry (not in an NSDL CAS) ---
    PPF = "ppf"
    EPF = "epf"
    PRIVATE_EQUITY = "private_equity"
    REAL_ESTATE = "real_estate"
    CASH = "cash"  # bank / FD / liquid
    OTHER = "other"


# CAS section a row was extracted from. Anchors classification before we even
# look at ISIN/description, because section headers are the stable part.
class Section(str, Enum):
    DEMAT = "demat"  # NSDL/CDSL demat holdings
    MUTUAL_FUND = "mutual_fund"  # MF folio summary (CAMS/KFintech)
    NPS = "nps"  # NPS holdings
    UNKNOWN = "unknown"


# Description keyword rules, evaluated in order. First match wins.
# Tune this table as real statements reveal new wording — it is the knob.
_KEYWORD_RULES: list[tuple[re.Pattern[str], AssetClass]] = [
    (re.compile(r"\b(sgb|sovereign\s+gold|gold\s+bond)\b", re.I), AssetClass.GOLD),
    (re.compile(r"\bgold\b.*\b(etf|fund)\b", re.I), AssetClass.GOLD),
    (re.compile(r"\b(ncd|debenture|bond)\b", re.I), AssetClass.DEBT),
    (re.compile(r"\b(g-?sec|govt?\.?\s+stock|treasury|t-?bill|gilt)\b", re.I), AssetClass.GOVT_SECURITY),
    (re.compile(r"\betf\b", re.I), AssetClass.ETF),
]


def classify(
    *,
    section: Section = Section.UNKNOWN,
    isin: str | None = None,
    description: str = "",
) -> AssetClass:
    """Classify a single holding into an AssetClass.

    Keyword arguments only, so call sites read self-documenting.
    """
    isin = (isin or "").strip().upper()
    desc = description or ""

    # 1. Section context — the strongest anchor.
    if section is Section.NPS:
        return AssetClass.NPS
    if section is Section.MUTUAL_FUND:
        return AssetClass.MUTUAL_FUND

    # 2/3. Within the demat section (or unknown), combine ISIN + description.
    #      Description keywords run first so a bond/gold row isn't mislabelled
    #      equity just because it carries an INE ISIN.
    for pattern, asset_class in _KEYWORD_RULES:
        if pattern.search(desc):
            return asset_class

    # 4. Fall back to ISIN prefix.
    if isin.startswith("INF"):
        return AssetClass.MUTUAL_FUND
    if isin.startswith(("IN00", "IN01", "IN02")):  # GoI security series
        return AssetClass.GOVT_SECURITY
    if isin.startswith("INE"):
        # Corporate issuer with no debt/ETF keyword -> treat as equity.
        return AssetClass.DIRECT_EQUITY

    return AssetClass.OTHER


# Human-readable labels for the UI.
LABELS: dict[AssetClass, str] = {
    AssetClass.DIRECT_EQUITY: "Direct Equity",
    AssetClass.MUTUAL_FUND: "Mutual Funds",
    AssetClass.DEBT: "Debt / Bonds",
    AssetClass.GOVT_SECURITY: "Govt Securities",
    AssetClass.GOLD: "Gold (SGB/ETF)",
    AssetClass.ETF: "ETFs",
    AssetClass.NPS: "NPS",
    AssetClass.PPF: "PPF",
    AssetClass.EPF: "EPF",
    AssetClass.PRIVATE_EQUITY: "Private Equity / Startup",
    AssetClass.REAL_ESTATE: "Real Estate",
    AssetClass.CASH: "Cash / FD",
    AssetClass.OTHER: "Unclassified",
}
