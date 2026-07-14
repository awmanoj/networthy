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

import io
import re
from datetime import date

import pdfplumber
import pikepdf

from ..classify import Section, classify
from ..models import Account, Holding, ParsedStatement


class CASParseError(Exception):
    """Raised when a CAS PDF cannot be decrypted or its key fields not found."""


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
    text = _extract_text(file_bytes, password)

    statement_date = _find_statement_date(text)
    total_value = _find_total_value(text)
    accounts = _find_accounts(text)

    return ParsedStatement(
        statement_date=statement_date,
        total_value=total_value,
        accounts=accounts,
        source_filename=source_filename,
    )


def _extract_text(file_bytes: bytes, password: str | None) -> str:
    """Decrypt (if needed) and return the full text of the PDF."""
    decrypted = _decrypt(file_bytes, password)
    try:
        with pdfplumber.open(io.BytesIO(decrypted)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - surface as a parse error
        raise CASParseError(f"Could not read PDF text: {exc}") from exc

    text = "\n".join(pages)
    if not text.strip():
        raise CASParseError(
            "PDF contained no extractable text (is it a scanned image?)."
        )
    return text


def _decrypt(file_bytes: bytes, password: str | None) -> bytes:
    """Return decrypted PDF bytes, or the original if it was not encrypted."""
    try:
        pdf = pikepdf.open(io.BytesIO(file_bytes), password=password or "")
    except pikepdf.PasswordError as exc:
        raise CASParseError(
            "Wrong or missing password for this CAS PDF."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise CASParseError(f"Could not open PDF: {exc}") from exc

    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


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
        if holding is None:
            continue

        if section is Section.DEMAT and (current is None or pending):
            flush_pending_demat()
        if current is None:
            current = _catch_all_account(section)
            accounts.append(current)
        current.holdings.append(holding)

    return [a for a in accounts if a.holdings]


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

    Anchors on the ISIN: text before it is the security/scheme name, and the
    trailing numeric tokens are read positionally as (units, price, value).
    """
    m = _ISIN_RE.search(line)
    if not m:
        return None
    isin = m.group(1)

    before = line[: m.start()].strip(" .:-\t")
    after = line[m.end():]

    # Two row shapes occur: "<name> <ISIN> <nums>" and "<ISIN> <name> <nums>".
    # In both, the numbers are the trailing tokens. Take the name from whichever
    # side carries the words, and never let the numeric tail bleed into it.
    if before:
        name = before
        numbers = _amounts(after) or _amounts(before)
    else:
        first_num = _HOLDING_NUM_RE.search(after)
        name = (after[: first_num.start()] if first_num else after).strip(" .:-\t")
        numbers = _amounts(after)
    name = name or isin

    units = price = value = None
    if len(numbers) >= 3:
        units, price, value = numbers[-3], numbers[-2], numbers[-1]
    elif len(numbers) == 2:
        units, value = numbers[0], numbers[-1]
    elif len(numbers) == 1:
        value = numbers[0]

    asset_class = classify(section=section, isin=isin, description=name)
    return Holding(
        name=_clean_name(name),
        asset_class=asset_class.value,
        isin=isin,
        units=units,
        price=price,
        value=value,
    )


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


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


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
