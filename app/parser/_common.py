"""Shared low-level helpers for the CAS parsers (NSDL, CAMS).

Both parsers decrypt a password-protected PDF, pull its text, and read
Indian-grouped rupee amounts. Those primitives live here so the format-specific
parsers stay focused on layout.
"""

from __future__ import annotations

import io

import pdfplumber
import pikepdf


class CASParseError(Exception):
    """Raised when a CAS PDF cannot be decrypted or its key fields not found."""


def decrypt(file_bytes: bytes, password: str | None) -> bytes:
    """Return decrypted PDF bytes, or the original if it was not encrypted."""
    try:
        pdf = pikepdf.open(io.BytesIO(file_bytes), password=password or "")
    except pikepdf.PasswordError as exc:
        raise CASParseError("Wrong or missing password for this CAS PDF.") from exc
    except Exception as exc:  # noqa: BLE001 - surface as a parse error
        raise CASParseError(f"Could not open PDF: {exc}") from exc

    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def extract_text(file_bytes: bytes, password: str | None) -> str:
    """Decrypt (if needed) and return the full text of the PDF."""
    decrypted = decrypt(file_bytes, password)
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


def to_float(raw: str) -> float | None:
    """Parse an Indian-grouped amount like '12,34,567.89' to float, else None."""
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None
