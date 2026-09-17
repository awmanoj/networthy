"""The free tracker spreadsheet and its landing page."""

import pathlib
import subprocess
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, main, prices, storage

PATH = "/net-worth-tracker-excel"
XLSX = pathlib.Path("app/static/networth-tracker.xlsx")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    import app.main as m
    return TestClient(m.app)


def test_page_is_public_and_indexable(client):
    assert client.get(PATH, follow_redirects=False).status_code == 200
    assert PATH in client.get("/sitemap.xml").text
    assert f"Allow: {PATH}" in client.get("/robots.txt").text


def test_the_file_is_actually_served(client):
    r = client.get(main.XLSX_FILE)
    assert r.status_code == 200
    assert r.content[:2] == b"PK"            # a real zip, i.e. a real xlsx
    assert len(r.content) > 5000


def test_the_page_offers_the_download_with_a_sensible_filename(client):
    body = client.get(PATH).text
    assert f'href="{main.XLSX_FILE}"' in body
    assert 'download="networthy-net-worth-tracker.xlsx"' in body


# --- The spreadsheet itself ---------------------------------------------------

def _sheet():
    openpyxl = pytest.importorskip("openpyxl")
    return openpyxl.load_workbook(XLSX)


def test_categories_match_the_calculator(client):
    """The spreadsheet, the web calculator and the product are one list. If they
    drift, someone downloads a tracker that doesn't match what they signed up for."""
    ws = _sheet()["Net worth"]
    in_sheet = {ws.cell(r, 1).value for r in range(1, ws.max_row + 1)}
    for row in main._calc_rows(main._CALC_ASSETS + main._CALC_LIABILITIES):
        assert row["title"] in in_sheet, row["title"]


def test_totals_and_net_worth_are_formulas_not_blanks():
    """The whole promise is 'you only ever type values'."""
    ws = _sheet()["Net worth"]
    formulas = [ws.cell(r, c).value
                for r in range(1, ws.max_row + 1) for c in (2, 3)
                if isinstance(ws.cell(r, c).value, str) and ws.cell(r, c).value.startswith("=")]
    assert any(f.startswith("=SUM(") for f in formulas)           # subtotals
    assert any(f == "=B18-B27" or f == "=C18-C27" for f in formulas)  # assets − liabilities
    # EDATE lives from column C on: B is the single date you set by hand.
    assert any(f.startswith("=EDATE(") for f in formulas)


def test_dates_self_populate_so_the_file_does_not_go_stale():
    ws = _sheet()["Net worth"]
    assert str(ws["B4"].value).startswith("=DATE(")
    assert ws["C4"].value == "=EDATE(B4,3)"       # each column derives from the one before


def test_it_uses_indian_digit_grouping():
    """A tracker for an Indian audience that renders ₹15,720,000 has missed the point."""
    ws = _sheet()["Net worth"]
    fmts = {ws.cell(r, 2).number_format for r in range(5, 30)}
    assert any("##\\,##\\,##\\,##0" in f for f in fmts)


def test_there_is_a_chart_because_the_line_is_the_point():
    assert len(_sheet()["Net worth"]._charts) == 1


def test_the_generator_is_reproducible_and_committed():
    """The xlsx is a build artefact committed to the repo; the script that makes it
    has to still run, or the next category change can't be applied."""
    pytest.importorskip("openpyxl")
    before = XLSX.read_bytes()
    try:
        r = subprocess.run([sys.executable, "scripts/build_tracker_xlsx.py"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert XLSX.exists() and XLSX.stat().st_size > 5000
    finally:
        XLSX.write_bytes(before)   # zip mtimes differ per run; don't dirty the tree
