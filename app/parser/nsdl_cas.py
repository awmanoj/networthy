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
from datetime import date, datetime

import pdfplumber
import pikepdf

from ..models import Holding, ParsedStatement


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
    holdings = _find_holdings(text)

    return ParsedStatement(
        statement_date=statement_date,
        total_value=total_value,
        holdings=holdings,
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


def _find_holdings(text: str) -> list[Holding]:
    """Best-effort per-line holdings extraction.

    TODO: NSDL CAS lays out holdings in tables per demat account and per mutual
    fund folio. Proper extraction should use pdfplumber's table detection
    (page.extract_tables) rather than line regex. For now we only capture ISIN
    lines so the dashboard can show a holding count.
    """
    holdings: list[Holding] = []
    for m in re.finditer(r"\b(IN[A-Z0-9]{10})\b", text):
        holdings.append(Holding(name=m.group(1), kind="other", isin=m.group(1)))
    return holdings


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
