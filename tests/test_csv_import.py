"""Importing holdings from any broker's CSV export.

One importer, not one per broker. The user confirms the column mapping, so a
format nobody has ever seen is resolved by a person picking from a dropdown
rather than by shipping a new parser — which is the whole reason this can't
break the way the CAS parser can.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, importer, prices, storage

# Real header shapes from real exports. The property that matters is the last
# one: an invented broker with ordinary names must work, because that's what
# "we haven't seen your format" means in practice.
SHEETS = {
    "zerodha": b"Symbol,ISIN,Sector,Quantity Available,Average Price,Previous Closing\n"
               b"RELIANCE,INE002A01018,Refineries,120,2400.50,2840.50\n",
    "icici": b"Stock Symbol,ISIN Code,Qty,Average Cost Price,Current Market Price,Current Value (INR )\n"
             b"RELIANCE,INE002A01018,120,2400.50,2840.50,340860.00\n",
    "groww": b"Stock Name,ISIN,Quantity,Avg. buy price,Market Value\n"
             b"Reliance Industries,INE002A01018,120,2400.50,340860\n",
    "trendlyne": b"Stock,ISIN,Qty,LTP,Cur. val,Invested\n"
                 b"INFY,INE009A01021,300,1560.00,468000,420000\n",
    "invented": b"Instrument,ISIN,Units Held,Rate,Worth\n"
                b"Infosys,INE009A01021,300,1560.00,468000\n",
}


@pytest.mark.parametrize("broker", sorted(SHEETS))
def test_every_real_export_reads(broker):
    sheet = importer.read_csv(SHEETS[broker])
    holdings, _ = importer.build(sheet, sheet.mapping)
    assert holdings, broker
    assert holdings[0].isin and holdings[0].value > 0


def test_market_value_beats_cost_basis():
    """Zerodha exports "Average Price" to the left of "Previous Closing". Picking
    the first match values the entire portfolio at cost — silently, in a tool
    whose whole job is what things are worth now."""
    sheet = importer.read_csv(SHEETS["zerodha"])
    holdings, _ = importer.build(sheet, sheet.mapping)
    assert holdings[0].value == pytest.approx(120 * 2840.50)   # not 120 * 2400.50


def test_cost_columns_are_used_only_when_nothing_else_exists():
    sheet = importer.read_csv(b"Scheme,ISIN,Units,Avg Cost\nHDFC Flexi,INF179K01WN9,100,50\n")
    holdings, _ = importer.build(sheet, sheet.mapping)
    assert holdings[0].value == pytest.approx(5000.0)


def test_a_title_block_above_the_table_is_skipped():
    """Broker exports routinely open with a title and an account number."""
    raw = (b"Holding Statement\nAccount: XXXX1234\n\n"
           b"Scheme Name,ISIN,Units,NAV,Market Value\n"
           b"HDFC Flexi Cap,INF179K01WN9,4210.554,1874.20,7891615\n")
    sheet = importer.read_csv(raw)
    assert sheet.headers[0] == "Scheme Name"
    holdings, _ = importer.build(sheet, sheet.mapping)
    assert holdings[0].value == pytest.approx(7891615)


def test_classification_runs_so_a_gold_fund_is_not_a_mutual_fund():
    """section=UNKNOWN on purpose, exactly as the CAMS parser does — otherwise a
    gold fund defaults to Mutual Funds and is counted in two leaves."""
    sheet = importer.read_csv(
        b"Fund Name,ISIN,Units,NAV,Current Value\nSBI Gold Fund,INF200K01SZ4,1000,25.5,25500\n")
    holdings, _ = importer.build(sheet, sheet.mapping)
    assert holdings[0].asset_class == "gold"


def test_indian_grouping_and_rupee_symbols_parse():
    assert importer.to_number("₹12,34,567.89") == pytest.approx(1234567.89)
    assert importer.to_number("") is None and importer.to_number("—") is None


def test_total_rows_and_valueless_rows_are_dropped():
    raw = (b"Name,ISIN,Value\nReliance,INE002A01018,340860\n"
           b"Suspended Co,,0\nTotal,,340860\n")
    sheet = importer.read_csv(raw)
    holdings, skipped = importer.build(sheet, sheet.mapping)
    assert [h.name for h in holdings] == ["Reliance"]
    assert "Suspended Co" in skipped


def test_a_malformed_isin_is_dropped_rather_than_stored():
    sheet = importer.read_csv(b"Name,ISIN,Value\nThing,NOTANISIN,1000\n")
    holdings, _ = importer.build(sheet, sheet.mapping)
    assert holdings[0].isin is None


@pytest.mark.parametrize("raw,msg", [
    (b"", "empty"),
    (b"Name,Notes\nReliance,good\n", "value"),
    (b"Price,Qty\n10,2\n", "name"),
])
def test_unusable_files_say_what_is_missing(raw, msg):
    with pytest.raises(importer.ImportError_) as e:
        sheet = importer.read_csv(raw)
        importer.build(sheet, sheet.mapping)
    assert msg in str(e.value).lower()


# --- End to end ---------------------------------------------------------------

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


def _login(email="csv@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def test_review_shows_the_guess_before_anything_is_saved(client):
    uid, ck = _login()
    body = client.post("/import/csv",
                       files={"file": ("h.csv", SHEETS["zerodha"], "text/csv")},
                       cookies=ck).text
    assert 'name="col_name"' in body            # every guess is editable
    assert "340,860" in body                    # and the total is shown to check
    assert storage.list_networth_holdings(uid, {"direct_equity"}) == []   # nothing written


def test_confirm_writes_and_reimport_replaces(client):
    uid, ck = _login("re@test.com")
    data = {"raw_csv": SHEETS["zerodha"].decode(), "col_name": "0", "col_isin": "1",
            "col_units": "3", "col_price": "5", "col_value": ""}
    client.post("/import/csv/confirm", data=data, cookies=ck)
    assert len(storage.list_networth_holdings(uid, {"direct_equity"})) == 1
    client.post("/import/csv/confirm", data=data, cookies=ck)
    assert len(storage.list_networth_holdings(uid, {"direct_equity"})) == 1   # not duplicated


def test_a_bad_upload_does_not_500(client):
    _, ck = _login("bad@test.com")
    r = client.post("/import/csv", files={"file": ("x.csv", b"\x00\x01\x02", "text/csv")},
                    cookies=ck)
    assert r.status_code == 200


# --- The thing that would actually cost money ---------------------------------

def test_the_same_fund_in_cams_and_csv_is_counted_once(client):
    """networth_holdings holds every import source at once, and merge_sources
    only deduped that list against NSDL — not against itself. A fund in both a
    CAMS statement and a broker CSV was emitted twice, straight into net worth."""
    import app.main as m
    rows = [{"isin": "INF179K01WN9", "name": "HDFC Flexi Cap", "value": 100.0, "source": "csv"},
            {"isin": "INF179K01WN9", "name": "HDFC FLEXI CAP", "value": 110.0, "source": "cams"}]
    merged = m.merge_sources(rows, [])
    assert len(merged) == 1
    assert merged[0]["value"] == 110.0            # the registrar's record wins
    assert merged[0]["source"] == m.SOURCE_CAMS


def test_a_csv_row_is_labelled_csv_not_cams(client):
    """The chip beside the ISIN is how de-duplication stays visible instead of
    being something the reader has to trust. It has to be true."""
    import app.main as m
    merged = m.merge_sources(
        [{"isin": "INE002A01018", "name": "Reliance", "value": 1.0, "source": "csv"}], [])
    assert merged[0]["source"] == m.SOURCE_CSV


def test_csv_and_nsdl_holding_the_same_stock_is_counted_once(client):
    import app.main as m
    merged = m.merge_sources(
        [{"isin": "INE002A01018", "name": "Reliance", "value": 340860.0, "source": "csv"}],
        [{"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES", "value": 330000.0}])
    assert len(merged) == 1 and merged[0]["source"] == m.SOURCE_CSV
