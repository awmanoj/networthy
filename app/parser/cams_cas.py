"""Parse a CAMS / KFintech Mutual-Fund Consolidated Account Statement (CAS).

Unlike the NSDL depository CAS, a CAMS CAS is **mutual-fund only**: it lists every
folio across all AMCs serviced by CAMS and KFintech, with per-scheme closing units,
NAV, and market value as of a valuation date. We pull exactly those, so the Networth
"Mutual Funds" (and "Gold & Silver", for gold/silver funds) pages can show real
holdings.

Two layouts are handled (both anchored on the ISIN):
  * **Consolidated Account Summary** — the common emailed CAS. One tabular row per
    scheme: ``<folio><ISIN> <name> <cost> <units> <dd-Mon-yyyy> <nav> <market value>
    <registrar>``, where the NAV *date* splits units (before) from NAV + value (after),
    and the scheme name wraps onto the next line(s). Note the folio is glued directly
    onto the ISIN, so ISIN detection can't rely on a leading word boundary.
  * **Detailed CAS** — an ISIN header line, then "Closing Unit Balance / NAV on <date>
    / Market Value on <date>" lines.

Classification reuses ``app.classify.classify`` with ``Section.UNKNOWN`` (NOT
``MUTUAL_FUND``) so its keyword rules run first: a "… Gold … Fund"/"… Silver … ETF"
name is tagged GOLD/SILVER, while everything else with an INF ISIN falls through to
MUTUAL_FUND. That's how gold/silver funds get routed to the Gold & Silver page.

The Summary path was validated against a real CAMS statement; the Detailed path is
still snippet-tested only. Column wording varies across issuers/periods, so the
extraction regexes are kept isolated for further hardening.
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

# ISIN = "IN" + 10 alphanumerics. No *leading* boundary: a Consolidated Account
# Summary glues the folio number straight onto the ISIN (e.g.
# "488487132216/0INF204K01562"), so we anchor only on the trailing edge (the ISIN
# must not be followed by another alphanumeric).
_ISIN_RE = re.compile(r"(IN[A-Z0-9]{10})(?![A-Z0-9])")

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

# Consolidated Account *Summary* (tabular) row: a "dd-Mon-yyyy" NAV date splits
# units (before it) from NAV + market value (after it).
_NUM_RE = re.compile(_NUM)
_ROW_DATE_RE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{4}")
# Lines that end a scheme's wrapped-name continuation (totals, page/section chrome).
_STOP_RE = re.compile(
    r"^(total\b|grand\s+total|page\s+\d|loads?\s+and\s+fees|consolidated\s+account|"
    r"as\s+on\b|folio\s+no\.?|\(inr\)|disclaimer)",
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
    """Walk the statement, emitting one Holding per scheme. Handles both layouts:

    * Consolidated Account **Summary** — one tabular row per scheme, where a
      "dd-Mon-yyyy" NAV date splits units (before) from NAV + market value (after);
      the scheme name wraps onto the following line(s).
    * Detailed CAS — an ISIN header line followed by "Closing Unit Balance / NAV on /
      Market Value on" lines.
    """
    holdings: list[Holding] = []
    current: dict | None = None      # detailed-format block accumulator
    last: Holding | None = None      # last summary holding, for name continuation

    def flush() -> None:
        nonlocal current
        if current and current.get("value") is not None:
            holdings.append(_holding_from(
                current["name"], current["isin"],
                current.get("units"), current.get("nav"), current["value"],
            ))
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _ISIN_RE.search(line)
        if m:
            flush()
            isin = m.group(1)
            row = _parse_summary_row(line[m.end():])
            if row is not None:
                holding = _holding_from(row["name"], isin, row["units"], row["nav"], row["value"])
                holdings.append(holding)
                last, current = holding, None
            else:
                # Detailed-format header: accumulate the block that follows.
                name = _TRAILING_ISIN_LABEL.sub("", line[: m.start()]).strip(" .:-\t")
                current, last = {"isin": isin, "name": name or isin}, None
            continue

        if current is not None:
            if (u := _first_amount(_UNITS_RE, line)) is not None:
                current["units"] = u
            if (n := _first_amount(_NAV_RE, line)) is not None:
                current["nav"] = n
            if (v := _first_amount(_MVAL_RE, line)) is not None:
                current["value"] = v
        elif last is not None:
            # Wrapped scheme-name continuation, until a totals/section marker.
            if _STOP_RE.search(line):
                last = None
            elif re.search(r"[A-Za-z]", line):
                last.name = f"{last.name} {line}".strip()

    flush()
    return holdings


def _parse_summary_row(after_isin: str) -> dict | None:
    """Parse the tabular columns after the ISIN, or None if it isn't a summary row.

    Layout: ``<scheme name> <cost> <units> <dd-Mon-yyyy> <nav> <market value> <registrar>``.
    The scheme name may itself contain numbers ("Nifty 50"), so we take the name as
    everything before the *last two* numbers ahead of the date (cost + units) rather
    than before the first number.
    """
    dm = _ROW_DATE_RE.search(after_isin)
    if not dm:
        return None
    head = after_isin[: dm.start()]
    tail = _amounts(after_isin[dm.end():])
    head_nums = list(_NUM_RE.finditer(head))
    if not head_nums or not tail:
        return None
    units = to_float(head_nums[-1].group(0))
    name_end = head_nums[-2].start() if len(head_nums) >= 2 else head_nums[-1].start()
    return {
        "name": head[:name_end].strip(" .:-\t"),
        "units": units,
        "nav": tail[0],
        "value": tail[-1],
    }


def _holding_from(name: str, isin: str, units, nav, value) -> Holding:
    asset_class = classify(section=Section.UNKNOWN, isin=isin, description=name)
    return Holding(
        name=name or isin, asset_class=asset_class.value, isin=isin,
        units=units, price=nav, value=value,
    )


def _amounts(fragment: str) -> list[float]:
    """Every numeric token in a fragment, left to right, as floats."""
    out: list[float] = []
    for m in _NUM_RE.finditer(fragment):
        v = to_float(m.group(0))
        if v is not None:
            out.append(v)
    return out


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
