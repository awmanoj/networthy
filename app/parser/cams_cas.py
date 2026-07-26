"""Parse a CAMS / KFintech Mutual-Fund Consolidated Account Statement (CAS).

Unlike the NSDL depository CAS, a CAMS CAS is **mutual-fund only**: it lists every
folio across all AMCs serviced by CAMS and KFintech, with per-scheme closing units,
NAV, and market value as of a valuation date. We pull exactly those, so the Networth
"Mutual Funds" (and "Gold & Silver", for gold/silver funds) pages can show real
holdings.

Layout notes (what we anchor on):
  * Each scheme block carries an **ISIN** (INF… for MF units) on its header line,
    usually right after the scheme name and an "ISIN" label.
  * The block then states "Closing Unit Balance: <units>", "NAV on <date>: <nav>",
    and "Market Value on <date>: <value>" — the three numbers we need.
  * The valuation date comes from the "… on <date>" phrases (or the statement period).

Classification reuses ``app.classify.classify`` with ``Section.UNKNOWN`` (NOT
``MUTUAL_FUND``) so its keyword rules run first: a "… Gold … Fund"/"… Silver … ETF"
name is tagged GOLD/SILVER, while everything else with an INF ISIN falls through to
MUTUAL_FUND. That's how gold/silver funds get routed to the Gold & Silver page.

Like ``nsdl_cas``, this is built to the *documented* CAMS layout and covered by
representative-text snippet tests — it still needs validation against a real CAMS PDF,
where column wording may vary and want hardening. The extraction regexes are kept
isolated for exactly that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..classify import Section, classify
from ..models import Holding
from ._common import CASParseError, extract_text, to_float

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ISIN: two letters (always "IN" for India) + 10 alphanumerics.
_ISIN_RE = re.compile(r"\b(IN[A-Z0-9]{10})\b")

# A number, Indian-grouped or plain, with any-length fraction (NAV/units carry 3-4).
_NUM = r"(?:\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"

# Per-scheme valuation fields. NAV/Market-Value lines embed a date we must step over
# before the amount, or the date's own digits would be read as the value.
_UNITS_RE = re.compile(r"closing\s+unit\s+balance\s*[:\-]?\s*(" + _NUM + r")", re.I)
_NAV_RE = re.compile(
    r"nav\s+on\s+\d{1,2}[-/ ][A-Za-z]{3,}[-/ ]\d{4}\s*[:\-]?\s*(?:inr|rs\.?)?\s*(" + _NUM + r")",
    re.I,
)
_MVAL_RE = re.compile(
    r"market\s+value\s+on\s+\d{1,2}[-/ ][A-Za-z]{3,}[-/ ]\d{4}\s*[:\-]?\s*(?:inr|rs\.?)?\s*(" + _NUM + r")",
    re.I,
)

_DATE_TOKEN = r"(\d{1,2})[-/ ]([A-Za-z]{3,})[-/ ](\d{4})"
# Valuation date, in order of preference.
_DATE_PATTERNS = [
    re.compile(r"(?:market\s+value|nav|valued?)\s+on\s+" + _DATE_TOKEN, re.I),
    re.compile(r"\bto\s+" + _DATE_TOKEN, re.I),
    re.compile(r"as\s+on\s+" + _DATE_TOKEN, re.I),
]

# Trailing "ISIN" label left on a name after we slice off the ISIN token itself.
_TRAILING_ISIN_LABEL = re.compile(r"\bISIN\b\s*[:\-]?\s*$", re.I)


@dataclass
class CamsImport:
    """The result of parsing one CAMS CAS."""

    holdings: list[Holding] = field(default_factory=list)
    as_of_date: date | None = None
    total_value: float = 0.0


def parse_cams(file_bytes: bytes, password: str | None = None) -> CamsImport:
    """Parse CAMS CAS PDF bytes into mutual-fund (and gold/silver-fund) holdings.

    Args:
        file_bytes: raw PDF content.
        password: the CAS PDF password (typically the PAN, in CAPITALS).

    Raises:
        CASParseError: on wrong/missing password or if no holdings are recognised.
    """
    text = extract_text(file_bytes, password)
    holdings = _parse_schemes(text)
    if not holdings:
        raise CASParseError(
            "No mutual-fund holdings found — is this a CAMS / KFintech CAS?"
        )
    return CamsImport(
        holdings=holdings,
        as_of_date=_find_statement_date(text),
        total_value=sum(h.value or 0.0 for h in holdings),
    )


def _parse_schemes(text: str) -> list[Holding]:
    """Walk the statement, emitting one Holding per scheme block.

    Anchors on the ISIN line as the scheme header; accumulates the block's closing
    units, NAV and market value; flushes a Holding when the next scheme starts.
    """
    holdings: list[Holding] = []
    current: dict | None = None
    last_text_line = ""

    def flush() -> None:
        nonlocal current
        if current and current.get("value") is not None:
            name = current["name"]
            asset_class = classify(
                section=Section.UNKNOWN, isin=current["isin"], description=name
            )
            holdings.append(
                Holding(
                    name=name,
                    asset_class=asset_class.value,
                    isin=current["isin"],
                    units=current.get("units"),
                    price=current.get("nav"),
                    value=current.get("value"),
                )
            )
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _ISIN_RE.search(line)
        if m:
            # An ISIN opens a new scheme block.
            flush()
            name = _TRAILING_ISIN_LABEL.sub("", line[: m.start()]).strip(" .:-\t")
            if not name:
                name = last_text_line
            current = {"isin": m.group(1), "name": name or m.group(1)}
        elif current is not None:
            if (u := _first_amount(_UNITS_RE, line)) is not None:
                current["units"] = u
            if (n := _first_amount(_NAV_RE, line)) is not None:
                current["nav"] = n
            if (v := _first_amount(_MVAL_RE, line)) is not None:
                current["value"] = v

        last_text_line = line

    flush()
    return holdings


def _first_amount(pattern: re.Pattern[str], line: str) -> float | None:
    m = pattern.search(line)
    return to_float(m.group(1)) if m else None


def _find_statement_date(text: str) -> date | None:
    """The valuation date, or None if not found (import still proceeds)."""
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        day, mid, year = m.groups()
        month = _MONTHS.get(mid[:3].lower()) if mid.isalpha() else None
        if not month and mid.isdigit():
            month = int(mid)
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                continue
    return None


# Convenience for quick manual testing:  python -m app.parser.cams_cas file.pdf PWD
if __name__ == "__main__":  # pragma: no cover
    import sys

    path = sys.argv[1]
    pwd = sys.argv[2] if len(sys.argv) > 2 else None
    with open(path, "rb") as fh:
        result = parse_cams(fh.read(), pwd)
    when = result.as_of_date.isoformat() if result.as_of_date else "unknown date"
    print(f"{when}  ₹{result.total_value:,.2f}  ({len(result.holdings)} holdings)")
    for h in result.holdings:
        print(f"  [{h.asset_class}] {h.name} — {h.units} @ {h.price} = ₹{h.value:,.2f}")
