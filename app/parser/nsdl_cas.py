"""Parse an NSDL CAS (Consolidated Account Statement) PDF.

An NSDL CAS is a password-protected PDF that consolidates, as of a statement
date, all of a PAN's holdings across:

  * NSDL & CDSL demat accounts (equities, bonds, ETFs)
  * Mutual fund folios (routed via CAMS / KFintech)

This module decrypts the PDF in memory, extracts its text, and pulls out the two
things Networthy needs for a snapshot: the **statement date** and the **total
portfolio value**. Per-holding extraction is best-effort and used only for a
holding count today.

The CAS layout is not perfectly stable across issuers/periods, so the extraction
patterns below are deliberately isolated and heavily commented — that is where
hardening against real statement variations should happen (see TODOs).
"""

from __future__ import annotations

import re
from datetime import date

from ..classify import Section, classify
from ..models import Account, Holding, ParsedStatement
from ._common import CASParseError, extract_text, to_float

# Kept as private aliases so this module's existing test surface (which imports
# `_to_float`) and callers importing `CASParseError` from here stay unchanged.
_to_float = to_float


# Indian-grouped rupee amounts, e.g. "12,34,567.89" or "1,000" or "45000.50".
_AMOUNT_RE = r"(?:\d{1,2},)?(?:\d{2},)*\d{3}(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"

# "as on 30-Jun-2024", "as on 30/06/2024", "as on 30-JUN-2024"
_DATE_PATTERNS = [
    re.compile(
        r"as on\s+(\d{1,2})[-/\s]([A-Za-z]{3,})[-/\s](\d{4})", re.IGNORECASE
    ),
    re.compile(r"as on\s+(\d{1,2})[-/](\d{1,2})[-/](\d{4})", re.IGNORECASE),
]

# The consolidated total goes by a few names across CAS variants.
_TOTAL_PATTERNS = [
    re.compile(
        r"(?:consolidated\s+)?(?:portfolio\s+value|total\s+value|grand\s+total)"
        r"[^\d]{0,40}?(" + _AMOUNT_RE + r")",
        re.IGNORECASE,
    ),
    re.compile(
        r"total[^\d\n]{0,20}?(" + _AMOUNT_RE + r")\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
]

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_cas(
    file_bytes: bytes,
    password: str | None = None,
    source_filename: str | None = None,
) -> ParsedStatement:
    """Parse CAS PDF bytes into a ParsedStatement.

    Args:
        file_bytes: raw PDF content.
        password: the CAS PDF password (typically the PAN). Optional if the PDF
            is not encrypted.
        source_filename: original filename, stored for reference only.

    Raises:
        CASParseError: on wrong/missing password or unrecognisable layout.
    """
    text = extract_text(file_bytes, password)

    statement_date = _find_statement_date(text)
    total_value = _find_total_value(text)
    accounts = _find_accounts(text)

    return ParsedStatement(
        statement_date=statement_date,
        total_value=total_value,
        accounts=accounts,
        source_filename=source_filename,
    )


def _find_statement_date(text: str) -> date:
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        day, mid, year = m.groups()
        month = _MONTHS.get(mid[:3].lower()) if mid.isalpha() else int(mid)
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                continue
    raise CASParseError(
        "Could not locate the statement date ('as on ...') in the CAS."
    )


def _find_total_value(text: str) -> float:
    for pattern in _TOTAL_PATTERNS:
        for m in pattern.finditer(text):
            value = _to_float(m.group(1))
            if value is not None and value > 0:
                return value
    raise CASParseError(
        "Could not locate the consolidated portfolio total in the CAS."
    )


# --- Detailed holding extraction --------------------------------------------
#
# A *detailed* NSDL CAS lays holdings out in tables, grouped into sections:
#
#   * one block per NSDL/CDSL demat account (a DP + client id), listing
#     ISIN · security name · balance · market price · value;
#   * mutual fund folios (statement-of-account form) grouped by AMC, listing
#     scheme · ISIN · closing units · NAV · value;
#   * an NPS block, if a PRAN is linked.
#
# pdfplumber flattens those tables to text lines, so we walk the lines keeping
# track of (a) which section we're in and (b) the current account, and treat any
# line carrying an ISIN as a holding row. Column *order* varies across CAS
# issuers/periods, so rather than pin fixed positions we anchor on the ISIN, take
# the text before it as the name, and read the trailing numbers positionally
# (…, units, price, value). This is the part most likely to need hardening
# against a real statement — keep it isolated and covered by snippet tests.

# ISIN: two letters (always "IN" for India) + 10 alphanumerics = 12 chars.
_ISIN_RE = re.compile(r"\b(IN[A-Z0-9]{10})\b")

# Section headers. The first that matches on a line switches the active section.
_SECTION_HEADERS: list[tuple[re.Pattern[str], Section]] = [
    (re.compile(r"national\s+pension\s+system|\bNPS\b", re.I), Section.NPS),
    (re.compile(r"mutual\s+fund\s+folios?|mutual\s+fund\s+units", re.I), Section.MUTUAL_FUND),
    (re.compile(r"national\s+securities\s+depository|central\s+depository|"
                r"demat\s+account|\bNSDL\b|\bCDSL\b", re.I), Section.DEMAT),
]

_DEPOSITORY_RE = re.compile(r"\b(NSDL|CDSL)\b", re.I)
# Account identifiers within a section.
_DP_NAME_RE = re.compile(r"DP\s*Name\s*[:\-]?\s*(.+?)\s*$", re.I)
_DP_ID_RE = re.compile(r"DP\s*ID\s*[:\-]?\s*([A-Z0-9]+)", re.I)
_CLIENT_ID_RE = re.compile(r"Client\s*ID\s*[:\-]?\s*([A-Z0-9]+)", re.I)
_FOLIO_RE = re.compile(r"Folio\s*(?:No\.?|Number)?\s*[:\-]?\s*([A-Z0-9/ ]+?)\s*$", re.I)
# AMC / fund-house line: an all-caps-ish name ending in a fund-house marker.
_AMC_RE = re.compile(r"^(.*\b(?:mutual\s+fund|amc|asset\s+management)\b.*)$", re.I)

# Numeric tokens *inside a holding row* need more decimal places than the money
# regex allows: MF NAVs carry 4 and unit balances 3, whereas _AMOUNT_RE caps at 2
# (which would split "500.123" into "500.12" + "3"). Indian-grouped or plain,
# with any-length fraction.
_HOLDING_NUM_RE = re.compile(r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _find_accounts(text: str) -> list[Account]:
    """Group the statement's holdings under their source accounts.

    Returns a list of Account, each carrying its Holding rows. Holdings whose
    section/account could not be pinned down still surface under a synthesised
    catch-all account so nothing is silently dropped.
    """
    accounts: list[Account] = []
    section = Section.UNKNOWN
    current: Account | None = None
    # The most recently emitted holding, so a wrapped scheme/security name spilling
    # onto the following line(s) can be stitched back on.
    last_holding: Holding | None = None
    # Pending demat-account descriptors, assembled across the header lines that
    # precede the first holding row of a block.
    pending: dict[str, str] = {}

    def flush_pending_demat() -> Account:
        nonlocal current, pending
        name = pending.get("dp_name") or "Demat account"
        ident_bits = [pending.get("dp_id"), pending.get("client_id")]
        identifier = " / ".join(b for b in ident_bits if b) or None
        current = Account(
            kind="demat",
            name=name,
            identifier=identifier,
            depository=pending.get("depository"),
        )
        accounts.append(current)
        pending = {}
        return current

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        new_section = _match_section(line)
        if new_section is not None:
            section = new_section
            last_holding = None  # a section boundary ends any name continuation
            # A depository name on this line seeds the next demat account.
            dep = _DEPOSITORY_RE.search(line)
            if new_section is Section.DEMAT and dep:
                pending["depository"] = dep.group(1).upper()
            elif new_section is not Section.DEMAT:
                current = None  # leaving demat; MF/NPS accounts open on their own headers
            # Header lines can *also* carry a holding row; fall through to parse it.

        # Accumulate account descriptors from header-ish lines.
        if section is Section.DEMAT:
            if m := _DP_NAME_RE.search(line):
                pending["dp_name"] = m.group(1).strip()
            if m := _DP_ID_RE.search(line):
                pending["dp_id"] = m.group(1)
            if m := _CLIENT_ID_RE.search(line):
                pending["client_id"] = m.group(1)
        elif section is Section.MUTUAL_FUND:
            if (m := _AMC_RE.match(line)) and not _ISIN_RE.search(line):
                current = Account(kind="mutual_fund", name=m.group(1).strip())
                accounts.append(current)
            if m := _FOLIO_RE.search(line):
                if current is None or current.kind != "mutual_fund":
                    current = Account(kind="mutual_fund", name="Mutual fund folio")
                    accounts.append(current)
                current.identifier = m.group(1).strip()

        holding = _parse_holding_line(line, section)
        if holding is not None:
            if section is Section.DEMAT and (current is None or pending):
                flush_pending_demat()
            if current is None:
                current = _catch_all_account(section)
                accounts.append(current)
            current.holdings.append(holding)
            last_holding = holding
            continue

        # Not a holding row. A ticker line ("E2E.NSE") printed right under an equity
        # row is that holding's exchange symbol — capture it (the key we use to fetch
        # a live price) and keep the continuation open for a trailing name tail.
        if last_holding is not None and _TICKER_RE.match(line):
            if last_holding.ticker is None:
                last_holding.ticker = line
        # A bare word-fragment right after a holding is its wrapped name tail (e.g. MF
        # "… FUND GROWTH" / "PLAN GROWTH OPTION"). Anything else — a header, a totals or
        # ISIN-bearing line — ends the continuation.
        elif last_holding is not None and _is_name_tail(line):
            last_holding.name = _clean_name(f"{last_holding.name} {line}")
        else:
            last_holding = None

    return [a for a in accounts if a.holdings]


# A stock ticker printed under an equity row, e.g. "E2E.NSE" / "RELIANCE.BSE" — not
# part of the security name.
_TICKER_RE = re.compile(r"^[A-Z0-9&]+\.[A-Z]{2,6}$")
# Lines that are never a wrapped name tail (headers, totals, label rows).
_NAME_STOP_RE = re.compile(
    r"^(total|grand\s+total|sub[-\s]?total|page\b|closing|opening|portfolio|"
    r"statement|disclaimer|summary|balance|isin\b|note\b)",
    re.I,
)


def _is_name_tail(line: str) -> bool:
    """True if `line` looks like a scheme/security name spilled onto its own line."""
    return bool(
        re.search(r"[A-Za-z]", line)          # has words
        and not _ISIN_RE.search(line)         # not another holding / ISIN prose line
        and ":" not in line                   # not a "DP Name : …" / "Folio : …" header
        and not _TICKER_RE.match(line)        # not a ticker
        and not _NAME_STOP_RE.search(line)    # not a totals/label row
    )


def _match_section(line: str) -> Section | None:
    for pattern, section in _SECTION_HEADERS:
        if pattern.search(line):
            return section
    return None


def _catch_all_account(section: Section) -> Account:
    kind = {
        Section.MUTUAL_FUND: "mutual_fund",
        Section.NPS: "nps",
    }.get(section, "demat")
    name = {"mutual_fund": "Mutual funds", "nps": "NPS"}.get(kind, "Demat holdings")
    return Account(kind=kind, name=name)


def _parse_holding_line(line: str, section: Section) -> Holding | None:
    """Turn a single ISIN-bearing line into a Holding, or None if it isn't one.

    Anchors on the ISIN. The value columns are the **trailing run of numbers** at the
    end of the line, read positionally as (units, price, value); the name is the words
    before that run (or, in the "<name> <ISIN> <cols>" shape, the text before the ISIN).

    Reading the *trailing* run — not "everything up to the first number" — keeps names
    that contain digits intact (the "2" in "E2E NETWORKS", the "50" in "Nifty 50") and
    drops lines that merely mention an ISIN in prose (e.g. "ISIN : INE255… - E2E …"),
    which have no trailing numeric block.
    """
    m = _ISIN_RE.search(line)
    if not m:
        return None
    isin = m.group(1)

    before = line[: m.start()].strip(" .:-\t")
    after = line[m.end():]

    num_start = _trailing_numbers_start(after)
    numbers = _amounts(after[num_start:]) if num_start is not None else []

    if before:
        name = before                                    # "<name> <ISIN> <cols>"
    elif num_start is not None:
        name = after[:num_start].strip(" .:-\t")          # "<ISIN> <name> <cols>"
    else:
        name = after.strip(" .:-\t")
    name = name or isin

    units = price = value = None
    if len(numbers) >= 3:
        units, price, value = numbers[-3], numbers[-2], numbers[-1]
    elif len(numbers) == 2:
        units, value = numbers[0], numbers[-1]
    elif len(numbers) == 1:
        value = numbers[0]

    # Interim hardening (HACK): drop rows with no positive value — a row whose amounts
    # wrapped to the next line, a bare "ISIN :" label/prose line (no trailing numbers),
    # or a nil/deleted holding shown as 0. They add nothing to any total.
    if not value:
        return None

    asset_class = classify(section=section, isin=isin, description=name)
    return Holding(
        name=_clean_name(name),
        asset_class=asset_class.value,
        isin=isin,
        units=units,
        price=price,
        value=value,
    )


def _trailing_numbers_start(text: str) -> int | None:
    """Index where the line's trailing run of numeric value columns begins.

    Returns None when the line does not end in a numeric block (so a digit embedded
    in a name, or an ISIN mentioned in prose, is not mistaken for a value). Numbers
    count as value columns only if they sit at the end of the line, separated from
    one another by non-letters.
    """
    matches = list(_HOLDING_NUM_RE.finditer(text))
    if not matches or re.search(r"[A-Za-z]", text[matches[-1].end():]):
        return None
    idx = len(matches) - 1
    while idx > 0 and not re.search(
        r"[A-Za-z]", text[matches[idx - 1].end(): matches[idx].start()]
    ):
        idx -= 1
    return matches[idx].start()


def _amounts(fragment: str) -> list[float]:
    """Every Indian-grouped amount in a text fragment, left to right."""
    out: list[float] = []
    for m in _HOLDING_NUM_RE.finditer(fragment):
        v = _to_float(m.group(0))
        if v is not None:
            out.append(v)
    return out


def _clean_name(name: str) -> str:
    """Tidy a raw security/scheme name pulled off a table row."""
    name = re.sub(r"\s{2,}", " ", name).strip(" .:-\t")
    return name


# Convenience for quick manual testing:  python -m app.parser.nsdl_cas file.pdf PWD
if __name__ == "__main__":  # pragma: no cover
    import sys

    path = sys.argv[1]
    pwd = sys.argv[2] if len(sys.argv) > 2 else None
    with open(path, "rb") as fh:
        result = parse_cas(fh.read(), pwd, source_filename=path)
    print(
        f"{result.statement_date.isoformat()}  "
        f"₹{result.total_value:,.2f}  "
        f"({result.holding_count} holdings)"
    )
