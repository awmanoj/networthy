"""The bilingual NSDL CAS (August 2026 generator) and the sections that are
not holdings.

Three things went wrong at once on real statements, and each gets a pin here:

* a Hindi translation in a hidden optional-content layer, drawn over the
  English, interleaves glyph by glyph in a text extractor ("ToTotatall");
* the visible layer sometimes repeats what the base content already says;
* the transaction statement and the expense-ratio table carry ISIN rows that
  read exactly like holdings, so the parser counted money that had moved as
  money that was held — on 124 of 134 real statements.

The PDFs here are built with pikepdf from the standard Helvetica font so no
real statement (and nothing from one) is needed.
"""

from __future__ import annotations

import io
from datetime import date

import pikepdf
import pytest

from app.parser import _common, nsdl_cas


# --- building tiny layered PDFs --------------------------------------------

def _pdf(base: str, layers: list[tuple[str, str, bool, float]], in_form=False) -> bytes:
    """A one-page PDF.

    `base` is drawn as plain content. Each layer is (name, text, hidden, dx):
    its text is drawn inside an optional-content block, shifted `dx` points,
    and the group is OFF by default when `hidden`. `text` may also be a list
    of (text, dx) segments drawn in one block. With `in_form` the whole
    page content lives in a Form XObject, as the 2026 generator writes it.
    """
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(400, 200))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica))
    groups, props, off = [], pikepdf.Dictionary(), []
    body = f"BT /F1 12 Tf 20 100 Td ({base}) Tj ET\n"
    for i, (name, text, hidden, dx) in enumerate(layers):
        g = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=name))
        groups.append(g)
        if hidden:
            off.append(g)
        props[f"/oc{i}"] = g
        segments = text if isinstance(text, list) else [(text, dx)]
        body += f"/OC /oc{i} BDC "
        for seg, sdx in segments:
            body += f"BT /F1 12 Tf {20 + sdx} 100 Td ({seg}) Tj ET "
        body += "EMC\n"
    pdf.Root.OCProperties = pikepdf.Dictionary(
        OCGs=pikepdf.Array(groups), D=pikepdf.Dictionary(OFF=pikepdf.Array(off)))
    resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font), Properties=props)
    if in_form:
        form = pdf.make_stream(body.encode())
        form.Type = pikepdf.Name.XObject
        form.Subtype = pikepdf.Name.Form
        form.BBox = pikepdf.Array([0, 0, 400, 200])
        form.Resources = resources
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(I1=form))
        page.Contents = pdf.make_stream(b"q /I1 Do Q")
    else:
        page.Resources = resources
        page.Contents = pdf.make_stream(body.encode())
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def _text(pdf_bytes: bytes) -> str:
    return _common.extract_text(pdf_bytes, None).strip()


# --- hidden layers are not read --------------------------------------------

def test_a_hidden_layer_is_not_extracted():
    pdf = _pdf("Grand Total 100.00", [("HINDI", "XXXXX XXXXX", True, 1.0)])
    assert _text(pdf) == "Grand Total 100.00"


def test_the_hidden_layer_is_stripped_inside_a_form_xobject():
    pdf = _pdf("Grand Total 100.00", [("HINDI", "XXXXX XXXXX", True, 1.0)], in_form=True)
    assert _text(pdf) == "Grand Total 100.00"


def test_without_stripping_the_layers_interleave():
    """What the real October 2026 statement looked like — the reason for all this."""
    pdf = _pdf("Grand Total 100.00", [("HINDI", "XXXXX XXXXX", True, 1.0)])
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        raw = doc.pages[0].extract_text()
    assert raw != "Grand Total 100.00"
    assert "X" in raw


def test_a_visible_layer_is_kept():
    pdf = _pdf("Grand Total 100.00", [("ENGLISH", "Equities (E)", False, 200.0)])
    assert _text(pdf) == "Grand Total 100.00 Equities (E)"


def test_a_visible_layer_that_echoes_the_base_is_collapsed():
    pdf = _pdf("Grand Total 100.00", [("ENGLISH", "Grand Total 100.00", False, 1.0)])
    assert _text(pdf) == "Grand Total 100.00"


def test_a_visible_layer_that_partly_echoes_keeps_only_its_new_words():
    """A table header the layer wraps differently: it repeats two cells and
    adds a third. The repeats go, the new cell stays."""
    pdf = _pdf("Account Type No. of ISINs",
               [("ENGLISH", [("Account Type", 1.0), ("Value in Rs", 250.0)], False, 0)])
    assert _text(pdf) == "Account Type No. of ISINs Value in Rs"


def test_a_file_with_no_layers_is_returned_byte_for_byte():
    pdf = _pdf("Grand Total 100.00", [])
    assert _common.visible_layers_only(pdf) is pdf


def test_layer_names_are_reported_for_the_diagnostic():
    pdf = _pdf("x", [("HINDI", "y", True, 0), ("ENGLISH", "z", False, 0)])
    assert _common.layer_names(pdf) == (["HINDI", "ENGLISH"], ["HINDI"])
    assert _common.layer_names(b"not a pdf") == ([], [])


def test_the_cleaner_never_raises():
    assert _common.visible_layers_only(b"not a pdf") == b"not a pdf"


# --- the holdings stop where the transactions begin -------------------------

_HOLDING = "INE002A01018 RELIANCE INDUSTRIES LIMITED 10.00 16 1,164.90 18,638.40"
_MOVED = "INE255Z01027 E2E NETWORKS LIMITED 1.00 100 520.40 52,040.00"


@pytest.mark.parametrize("heading", [
    "Transactions",
    "Summary of Transactions of",
    "NSDL Demat Account Summary of Transactions",
    "Mutual Funds Transaction Statement for the Period from 01-Oct-2026 to 31-Oct-2026",
])
def test_rows_after_the_transactions_heading_are_not_holdings(heading):
    text = f"NSDL Demat Account\n{_HOLDING}\n{heading}\n{_MOVED}\n"
    rows = [h for a in nsdl_cas._find_accounts(text) for h in a.holdings]
    assert [h.isin for h in rows] == ["INE002A01018"]


def test_the_transactions_tab_in_the_page_header_is_not_a_heading():
    """Every page carries 'Summary Holdings Transactions Your Account About
    NSDL' — a navigation strip, not the section, and it also says NSDL."""
    text = ("Mutual Fund Folios (F)\n"
            "INF846K01K01 Axis Small Cap Fund 46,098.089 97.21 44,81,195.23\n"
            "Summary Holdings Transactions Your Account About NSDL\n"
            "INF174K01187 Kotak Equity Opportunities 20,086.035 317.824 63,83,823.99\n")
    accounts = nsdl_cas._find_accounts(text)
    assert len(accounts) == 1 and accounts[0].kind == "mutual_fund"
    assert len(accounts[0].holdings) == 2


def test_expense_ratio_rows_are_skipped():
    text = ("Mutual Fund Folios (F)\n"
            f"{_HOLDING}\n"
            "Total Expense Ratio (**) & Commission Paid\n"
            "APR-26 TO SEP-26 INF846K01K01 Axis Small Cap Fund ARN-27988 Peakalpha 1.85 1,234.56\n")
    rows = [h for a in nsdl_cas._find_accounts(text) for h in a.holdings]
    assert [h.isin for h in rows] == ["INE002A01018"]


# --- the date can come from the statement period ----------------------------

def test_the_statement_date_falls_back_to_the_period_end():
    text = "Statement for the period from 01-May-2025 to 31-May-2025"
    assert nsdl_cas._find_statement_date(text) == date(2025, 5, 31)


def test_as_on_still_wins_over_the_period():
    text = ("Statement for the period from 01-May-2025 to 31-May-2025\n"
            "Portfolio as on 30-May-2025")
    assert nsdl_cas._find_statement_date(text) == date(2025, 5, 30)
