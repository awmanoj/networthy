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


# How far to look for a holding row's numbers when they didn't land on the ISIN's
# own line. Two is enough for every wrap seen in real statements and small enough
# that it can't reach into the next holding.
_WRAP_LOOKAHEAD = 2
# How far back to look for a row's description. Three lines covers the longest
# wrapped AIF name seen in a real statement without reaching the row above.
_WRAP_LOOKBEHIND = 3
# Column headers and running totals — never part of a security's name.
# "ISIN : INF… - Scheme Name : …" is a *label* in the transaction statement, not
# a holding. It was harmless before rejoining, because it carries no trailing
# numbers and so parsed to nothing — but the line under it is "Folio-no - 7499746",
# which does. Gluing the two makes a phantom holding worth its own folio number.
_PROSE_ISIN_RE = re.compile(r"\bISIN\s*[:\-]|\bScheme\s+Name\s*:|\bFolio-?\s*no\b", re.I)
_HEADER_HINT_RE = re.compile(
    r"\b(ISIN\s+Description|No\.?\s*of\s*(Units|Shares)|Market\s+Price|"
    r"Face\s+Value\s+in|Stock\s+Symbol|Value\s+in|Sub\s*Total|Grand\s*Total|"
    r"NAV\s+in|Company\s+Name)\b", re.I)


def _rejoin_wrapped_rows(lines: list[str]) -> list[str]:
    """Put holding rows back together when the PDF split them across lines.

    pdfplumber flattens a table row to text, and a narrow column makes it wrap.
    Real statements do this to the equity block in particular:

        INE002A01018
        BAJAJ FINANCE LIMITED   5.00   12   8,123.45   97,481.40

    The ISIN lands alone and every number is on the next line, so
    `_parse_holding_line` sees a row with no value and drops it — silently, and
    for the whole section. That is how an entire equity holding list can vanish
    from an otherwise successful parse.

    So: when a line carries an ISIN but no trailing numeric block, pull in the
    following line if that one ends in numbers. Both wrap shapes seen in the
    wild are covered — the ISIN alone, and the ISIN with its name attached.
    """
    out: list[str] = []
    consumed = -1
    for i, line in enumerate(lines):
        if i <= consumed:
            continue
        stripped = line.strip()
        if _PROSE_ISIN_RE.search(stripped):
            out.append(stripped)          # a label; never joined, never a holding
            continue
        m = _ISIN_RE.search(stripped)
        if m and _trailing_numbers_start(_mask_face_values(stripped[m.end():])) is None:
            for j in range(i + 1, min(i + 1 + _WRAP_LOOKAHEAD, len(lines))):
                nxt = lines[j].strip()
                if not nxt or _ISIN_RE.search(nxt):
                    break          # blank, or the next holding started — give up
                if _trailing_numbers_start(_mask_face_values(nxt)) is not None:
                    stripped = f"{stripped} {nxt}"
                    consumed = j
                    break
            m = _ISIN_RE.search(stripped)

        # The other wrap shape: the row is "ISIN units nav value" and the security's
        # *description* is on the lines above it, because the name column is narrow
        # and the table is read top-down. Without this the holding is named after
        # its own ISIN — and, worse, classification loses the only evidence it has:
        # "CATEGORY-I" and "Restricted Transferability" live in that description,
        # and they are what separate an AIF from a mutual fund.
        if m and not _row_has_a_name(stripped, m):
            lead = _leading_description(lines, i)
            if lead:
                stripped = f"{lead} {stripped}"
        out.append(stripped)
    return out


def _row_has_a_name(line: str, m: re.Match[str]) -> bool:
    """Does this row already carry a security name, before or after its ISIN?

    Both shapes count. The equity block reads "<ISIN> <NAME> <numbers>" once the
    wrapped numbers are rejoined, and looking further back there would pull in the
    *previous* holding's line — which is how a row ends up named after its
    neighbour's figures.
    """
    if line[: m.start()].strip(" .:-\t"):
        return True
    after = line[m.end():]
    num_start = _trailing_numbers_start(_mask_face_values(after))
    middle = after if num_start is None else after[:num_start]
    return bool(re.search(r"[A-Za-z]", middle))


def _leading_description(lines: list[str], idx: int) -> str:
    """Description lines immediately above a data row, joined top-down.

    Bounded by anything that ends the description: a blank line, another ISIN
    (the previous holding), a column header, or a line that is only numbers —
    which is the previous row's wrapped tail, not this row's name.
    """
    picked: list[str] = []
    for j in range(idx - 1, max(-1, idx - 1 - _WRAP_LOOKBEHIND), -1):
        prev = lines[j].strip()
        if not prev or _ISIN_RE.search(prev):
            break
        if _HEADER_HINT_RE.search(prev):
            break
        if not re.search(r"[A-Za-z]", prev):
            break
        picked.append(prev)
    return " ".join(reversed(picked))


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

    for raw_line in _rejoin_wrapped_rows(text.splitlines()):
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


# How close units x price has to land to value before we believe a column triple.
# Statements round units to 3 decimals and NAVs to 4, so exact equality never
# holds; 1% is loose enough for that and far tighter than the gap between a
# market value and any other column on the row.
_COL_TOLERANCE = 0.01


def _pick_columns(numbers: list[float]) -> tuple[float | None, float | None, float | None]:
    """Choose (units, price, value) from a row's numeric columns.

    Reading the last three positionally works for the demat table — face value,
    quantity, price, value — but not for mutual-fund folios, which carry seven
    columns and end with *unrealised gain*:

        folio | units | avg cost | cost value | NAV | market value | gain

    There the last three are (NAV, market value, gain), so the holding gets
    filed at its gain and the section totals come up short — quietly, because
    every row still parses.

    So instead of trusting position, look for a triple that is internally
    consistent: units x price == value. A row's real figures satisfy it and a
    mis-aligned reading almost never does. Where several do — cost value also
    equals units x avg cost — take the **rightmost**, which is the market value
    rather than what was paid.

    Falls back to the positional reading when nothing is consistent, so layouts
    with only a value, or with columns we don't recognise, behave as before.
    """
    n = len(numbers)
    for v_i in range(n - 1, 1, -1):
        value = numbers[v_i]
        if value <= 0:
            continue
        for p_i in range(v_i - 1, 0, -1):
            price = numbers[p_i]
            if price <= 0:
                continue
            for u_i in range(p_i - 1, -1, -1):
                units = numbers[u_i]
                if units <= 0:
                    continue
                if abs(units * price - value) <= _COL_TOLERANCE * value:
                    return units, price, value

    if n >= 3:
        return numbers[-3], numbers[-2], numbers[-1]
    if n == 2:
        return numbers[0], None, numbers[-1]
    if n == 1:
        return None, None, numbers[0]
    return None, None, None


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

    masked = _mask_face_values(after)
    num_start = _trailing_numbers_start(masked)
    numbers = _amounts(after[num_start:]) if num_start is not None else []

    if before:
        name = before                                    # "<name> <ISIN> <cols>"
    elif num_start is not None:
        name = after[:num_start].strip(" .:-\t")          # "<ISIN> <name> <cols>"
    else:
        name = after.strip(" .:-\t")
    name = name or isin

    units, price, value = _pick_columns(numbers)

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


# NSDL writes an AIF's face value into the security name itself — "…-FACE VALUE
# INR 100.0/- AND PAID UP". Those digits sit immediately before the real data
# columns, and when a row carries only two of them (units and value: an unlisted
# AIF has no market price) the positional read takes the face value as units and
# shifts everything along by one.
#
# Masking the digits inside that idiom — same-length, so every index still lines
# up — makes them invisible to the column scan. The name is sliced from the
# original text, so it keeps the real wording.
# "FACE VALUE INR 100.0/-" and the bare "…AND PAID UP VALUE INR 100.0/" that can
# follow it — both are name text, neither is a column.
_FACE_VALUE_RE = re.compile(
    r"((?:face\s+)?value\s*(?:inr|rs\.?)\s*)([\d][\d.,]*)", re.I
)


# Money in a CAS is always written with decimals ("15,234.56") or Indian grouping,
# never as a long bare run of digits. Folio and account numbers are exactly that,
# and they sit on the same flattened line as the holding — so without this a
# 12-digit folio number is read as a rupee value. That produced a real ₹477 billion
# "holding" with no units and no price.
#
# 9 digits is the floor because it's already ₹10 crore: past anything a statement
# would print without separators, and short of the folio lengths seen in practice.
_ID_DIGITS = 9


def _looks_like_identifier(token: str) -> bool:
    """True for a bare digit run long enough to be a folio/account number."""
    return (
        "." not in token
        and "," not in token
        and len(token) >= _ID_DIGITS
    )


def _mask_face_values(text: str) -> str:
    """Hide face-value digits from the column scan, preserving string length."""
    return _FACE_VALUE_RE.sub(lambda m: m.group(1) + "x" * len(m.group(2)), text)


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
    """Every Indian-grouped amount in a text fragment, left to right.

    Bare digit runs long enough to be identifiers (folio numbers, account
    numbers) are skipped — they are not amounts, and reading one as a value
    produces a holding worth hundreds of crore.
    """
    out: list[float] = []
    for m in _HOLDING_NUM_RE.finditer(fragment):
        token = m.group(0)
        if _looks_like_identifier(token):
            continue
        v = _to_float(token)
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
