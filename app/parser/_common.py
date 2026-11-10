"""Shared low-level helpers for the CAS parsers (NSDL, CAMS).

Both parsers decrypt a password-protected PDF, pull its text, and read
Indian-grouped rupee amounts. Those primitives live here so the format-specific
parsers stay focused on layout.
"""

from __future__ import annotations

import io
import re

import pdfplumber
import pikepdf
from pdfplumber.utils import cluster_objects, extract_text as _text_of_chars
from pdfplumber.utils import extract_words as _words_of_chars


class CASParseError(Exception):
    """Raised when a CAS PDF cannot be decrypted or its key fields not found.

    Carries a `cause` so the UI can offer the right way out without matching on
    message text. Four of the five are the user's to fix in seconds; only
    `layout` is ours, and that one must never be a dead end — a statement we
    can't read is the single most likely reason someone tries this app once and
    never comes back.
    """

    #: password  — wrong or missing PDF password (by far the most common)
    #: scanned   — a PDF with no extractable text
    #: unreadable— corrupt, or not a PDF at all
    #: layout    — opened and read fine, but the shape is one we don't know
    #: unknown   — anything else
    def __init__(self, message: str, cause: str = "unknown"):
        super().__init__(message)
        self.cause = cause


def decrypt(file_bytes: bytes, password: str | None) -> bytes:
    """Return decrypted PDF bytes, or the original if it was not encrypted."""
    try:
        pdf = pikepdf.open(io.BytesIO(file_bytes), password=password or "")
    except pikepdf.PasswordError as exc:
        raise CASParseError("Wrong or missing password for this CAS PDF.",
                            cause="password") from exc
    except Exception as exc:  # noqa: BLE001 - surface as a parse error
        raise CASParseError("This file could not be opened as a PDF.",
                            cause="unreadable") from exc

    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def extract_text(file_bytes: bytes, password: str | None) -> str:
    """Decrypt (if needed) and return the full text of the PDF.

    Reads only what a viewer would show: content in a hidden optional-content
    layer is removed first (`visible_layers_only`), and a visible layer that
    merely repeats the base content is collapsed (`_page_text`). See the notes
    on those two for the NSDL generator that made this necessary.
    """
    decrypted = visible_layers_only(decrypt(file_bytes, password))
    try:
        with pdfplumber.open(io.BytesIO(decrypted)) as pdf:
            pages = [_page_text(page) for page in pdf.pages]
    except Exception as exc:  # noqa: BLE001 - surface as a parse error
        raise CASParseError("This PDF could not be read.", cause="unreadable") from exc

    text = "\n".join(pages)
    if not text.strip():
        raise CASParseError(
            "This PDF has no text in it — it looks like a scan or a photo.",
            cause="scanned",
        )
    return text


def to_float(raw: str) -> float | None:
    """Parse an Indian-grouped amount like '12,34,567.89' to float, else None."""
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


# --- Optional-content layers ----------------------------------------------
#
# From August 2026 NSDL's CAS generator (PDFlib, wrapped by iText 9) writes the
# statement bilingually: a Hindi translation sits in a PDF optional-content
# group (OCG) named HINDI, switched OFF by default, drawn over the same
# positions as an ENGLISH group. A viewer shows English. A text extractor
# ignores layer visibility and sees both, interleaved glyph by glyph —
# "Total" becomes "ToTotatall" and the portfolio value "1155,,8800,,…". The
# holding rows survive (they are not translated); every heading, total and
# section marker does not, which is what took the October 2026 statement from
# "parses" to "could not find the portfolio total".
#
# The fix is to do what the viewer does: drop every `/OC /x BDC … EMC` block
# whose group is hidden, in every content stream (the page and its Form
# XObjects — the new generator puts the whole page inside one), *before*
# pdfplumber sees the file. It is exact rather than statistical, and a file
# with no layers is returned untouched. Nothing in it raises: a cleaner that
# fails must fall back to the raw bytes, not take the parse down with it.


def visible_layers_only(pdf_bytes: bytes) -> bytes:
    """Return the PDF with content in hidden optional-content groups removed."""
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        hidden = _hidden_ocgs(pdf)
        if not hidden:
            return pdf_bytes
        seen: set = set()
        for page in pdf.pages:
            _strip_hidden(pdf, page, hidden, seen)
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - best effort, never fatal
        return pdf_bytes


def layer_names(pdf_bytes: bytes) -> tuple[list[str], list[str]]:
    """(all optional-content group names, the ones hidden by default)."""
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        ocp = pdf.Root.get("/OCProperties")
        if ocp is None:
            return [], []
        hidden = _hidden_ocgs(pdf)
        groups = list(ocp.get("/OCGs", []))
        names = [str(g.get("/Name", "?")) for g in groups]
        off = [str(g.get("/Name", "?")) for g in groups if g.objgen in hidden]
        return names, off
    except Exception:  # noqa: BLE001
        return [], []


def _hidden_ocgs(pdf: pikepdf.Pdf) -> set:
    ocp = pdf.Root.get("/OCProperties")
    if ocp is None:
        return set()
    return {g.objgen for g in ocp.get("/D", {}).get("/OFF", [])}


def _ocg_members(ref) -> list:
    """The groups a BDC property or an XObject's /OC entry refers to."""
    if ref is None:
        return []
    if str(ref.get("/Type", "")) == "/OCMD":
        groups = ref.get("/OCGs")
        if groups is None:
            return []
        return list(groups) if isinstance(groups, pikepdf.Array) else [groups]
    return [ref]


def _strip_hidden(pdf: pikepdf.Pdf, holder, hidden: set, seen: set) -> None:
    """Rewrite one content stream (a page or a form) without its hidden blocks."""
    resources = holder.get("/Resources", {})
    props = resources.get("/Properties", {})
    xobjects = resources.get("/XObject", {})
    kept, depth, skipping = [], 0, 0
    for operands, operator in pikepdf.parse_content_stream(holder):
        op = str(operator)
        if op == "BDC":
            depth += 1
            if not skipping and len(operands) == 2 and str(operands[0]) == "/OC":
                ref = props.get(str(operands[1]))
                if any(g.objgen in hidden for g in _ocg_members(ref)):
                    skipping = depth
        elif op == "EMC":
            if skipping == depth:
                skipping = 0
                depth -= 1
                continue
            depth -= 1
        elif op == "Do" and not skipping:
            form = xobjects.get(str(operands[0]))
            if form is not None and str(form.get("/Subtype", "")) == "/Form":
                if any(g.objgen in hidden for g in _ocg_members(form.get("/OC"))):
                    continue  # the whole form is a hidden layer
                if form.objgen not in seen:
                    seen.add(form.objgen)
                    _strip_hidden(pdf, form, hidden, seen)
        if not skipping:
            kept.append((operands, operator))
    data = pikepdf.unparse_content_stream(kept)
    if isinstance(holder, pikepdf.Page) or str(holder.get("/Type", "")) == "/Page":
        holder.Contents = pdf.make_stream(data)
    else:
        holder.write(data)


# The visible ENGLISH layer is not always the only copy either: the October
# 2026 statement draws each label in the page's base content *and* again
# inside the layer, a point or so apart (or re-set in another weight), so
# even with Hindi gone "Grand Total" reads "GGrraanndd TTootataall". A viewer
# overprints them. So, per text line: if what the layer says is already said
# by the base content, the layer's glyphs go; where the layer wraps a table
# header differently, only the words it repeats go. pdfplumber marks a glyph
# inside any marked-content block with tag "OC", which after the hidden
# layer is stripped means "inside the visible layer".

_Y_TOL = 3
_X_TOL = 3


def _page_text(page) -> str:
    chars = page.dedupe_chars(tolerance=1).chars   # same-font overprint ("bold")
    return _text_of_chars(_collapse_layer_echo(chars),
                          x_tolerance=_X_TOL, y_tolerance=_Y_TOL) or ""


def _collapse_layer_echo(chars: list) -> list:
    out: list = []
    for line in cluster_objects(chars, lambda c: c["top"], _Y_TOL):
        base = [c for c in line if c.get("tag") != "OC"]
        layer = [c for c in line if c.get("tag") == "OC"]
        if not base or not layer:
            out.extend(line)
            continue
        b, l = _squash(base), _squash(layer)
        if l and (l in b or b in l):
            out.extend(base if len(b) >= len(l) else layer)
            continue
        base_words = {(w["text"], round(w["x0"] / 8)) for w in _words(base)}
        out.extend(base)
        for w in _words(layer):
            if not any((w["text"], round((w["x0"] + d) / 8)) in base_words
                       for d in (-8, 0, 8)):
                out.extend(w["chars"])
    return out


def _squash(chars: list) -> str:
    text = "".join(c["text"] for c in sorted(chars, key=lambda c: c["x0"]))
    return re.sub(r"\s+", "", text).lower()


def _words(chars: list) -> list:
    return _words_of_chars(chars, x_tolerance=_X_TOL, y_tolerance=_Y_TOL,
                           return_chars=True)
