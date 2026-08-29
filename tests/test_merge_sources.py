"""Merging an NSDL CAS with a CAMS import.

Both statements can carry the same mutual fund, so the two failure modes are
counting it twice and — the one that actually shipped — dropping it. Mutual Funds
used to take `cams or nsdl`, replacing the NSDL rows wholesale the moment any
CAMS import existed, which silently deleted every fund CAMS didn't service.
Money vanishing is worse than money duplicated: nothing on screen says it
happened.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage
from app.main import SOURCE_CAMS, SOURCE_NSDL, merge_sources
from app.models import Account, Holding, Snapshot


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


def _login(email="merge@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _row(name, isin, value):
    return {"name": name, "isin": isin, "value": value, "asset_class": "mutual_fund"}


# --- The merge itself -------------------------------------------------------

def test_same_isin_appears_once_with_cams_winning():
    merged = merge_sources(
        [_row("HDFC Flexi Cap", "INF001", 110_000.0)],
        [_row("HDFC FLEXI CAP FUND", "INF001", 100_000.0)],
    )
    assert len(merged) == 1
    assert merged[0]["value"] == 110_000.0          # the registrar's own figure
    assert merged[0]["source"] == SOURCE_CAMS


def test_same_fund_without_an_isin_is_matched_by_name():
    """A row missing an ISIN would otherwise never match and would be added
    beside its own duplicate — the double-count this guards."""
    merged = merge_sources(
        [_row("AXIS BLUECHIP FUND-DIRECT-GROWTH", None, 55_000.0)],
        [_row("Axis Bluechip Fund - Direct Growth", None, 50_000.0)],
    )
    assert len(merged) == 1
    assert merged[0]["value"] == 55_000.0


def test_a_fund_only_in_the_nsdl_cas_survives():
    """The bug that shipped: this row used to disappear entirely once a CAMS
    import existed."""
    merged = merge_sources(
        [_row("HDFC Flexi Cap", "INF001", 110_000.0)],
        [_row("Quant Small Cap", "INF999", 100_000.0)],
    )
    assert {r["name"] for r in merged} == {"HDFC Flexi Cap", "Quant Small Cap"}
    assert [r["source"] for r in merged] == [SOURCE_CAMS, SOURCE_NSDL]


def test_two_different_funds_from_one_source_are_both_kept():
    merged = merge_sources([], [_row("A Fund", "INF1", 1.0), _row("B Fund", "INF2", 2.0)])
    assert len(merged) == 2
    assert all(r["source"] == SOURCE_NSDL for r in merged)


def test_nsdl_duplicates_within_itself_collapse():
    merged = merge_sources([], [_row("A Fund", "INF1", 1.0), _row("A FUND", "INF1", 1.0)])
    assert len(merged) == 1


def test_the_merge_never_mutates_its_inputs():
    cams = [_row("A", "INF1", 1.0)]
    merge_sources(cams, [])
    assert "source" not in cams[0], "tagging leaked back into the caller's rows"


# --- End to end -------------------------------------------------------------

def test_a_cams_import_does_not_delete_nsdl_held_funds(client):
    uid, ck = _login()
    import app.main as m

    class U:
        id = uid

    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=0.0,
        holding_count=2, source_filename="cas.pdf"))
    storage.replace_holdings(sid, [Account(kind="demat", name="NSDL", holdings=[
        Holding("HDFC FLEXI CAP FUND", "mutual_fund", "INF001", 100.0, 1000.0, 100_000.0),
        Holding("QUANT SMALL CAP FUND", "mutual_fund", "INF999", 50.0, 2000.0, 100_000.0),
    ])])
    assert m._leaf_value(U, "mutual-funds") == 200_000.0

    # CAMS services only the first fund.
    storage.replace_networth_import(uid, "cams", date(2026, 7, 31), [
        Holding("HDFC FLEXI CAP FUND", "mutual_fund", "INF001", 100.0, 1100.0, 110_000.0),
    ])
    # CAMS value for the overlap + the NSDL-only fund still present.
    assert m._leaf_value(U, "mutual-funds") == 210_000.0


def test_the_leaf_page_shows_where_each_row_came_from(client):
    uid, ck = _login("chips@test.com")
    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=0.0,
        holding_count=1, source_filename="cas.pdf"))
    storage.replace_holdings(sid, [Account(kind="demat", name="NSDL", holdings=[
        Holding("QUANT SMALL CAP FUND", "mutual_fund", "INF999", 50.0, 2000.0, 100_000.0),
    ])])
    storage.replace_networth_import(uid, "cams", date(2026, 7, 31), [
        Holding("HDFC FLEXI CAP FUND", "mutual_fund", "INF001", 100.0, 1100.0, 110_000.0),
    ])

    page = client.get("/networth/assets/financial-assets/mutual-funds", cookies=ck).text
    assert "src-cams" in page and "src-cas" in page


# --- Gold funds counted twice across two leaves ------------------------------
#
# The subtle one. merge_sources de-duplicates *within* a leaf, so it can't help
# when the same holding is filed into two different leaves — and that's exactly
# what happened: classify() returned early on the mutual-fund section context, so
# an NSDL MF-folio section called a gold fund `mutual_fund` while CAMS (which
# passes section=UNKNOWN on purpose) called the same fund `gold`.

def test_the_same_gold_fund_is_not_counted_in_two_leaves(client):
    uid, _ck = _login("gold@test.com")
    import app.main as m

    class U:
        id = uid

    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=0.0,
        holding_count=1, source_filename="cas.pdf"))
    storage.replace_holdings(sid, [Account(kind="mutual_fund", name="MF Folios", holdings=[
        Holding("SBI GOLD FUND - DIRECT GROWTH", "mutual_fund",
                "INF200K01T28", 100.0, 60.0, 600_000.0),
    ])])
    storage.replace_networth_import(uid, "cams", date(2026, 7, 31), [
        Holding("SBI GOLD FUND - DIRECT GROWTH", "gold",
                "INF200K01T28", 100.0, 62.0, 620_000.0),
    ])
    storage.reclassify_holdings()

    assert m._leaf_value(U, "mutual-funds") == 0.0        # was 600,000
    assert m._leaf_value(U, "gold-silver") == 620_000.0   # CAMS wins, counted once


def test_both_parsers_agree_on_a_gold_fund():
    """The root cause in one line: the two sources must not disagree about what
    a gold fund is, or it lands in two leaves and is counted twice."""
    from app.classify import Section, classify
    for name in ("SBI GOLD FUND - DIRECT GROWTH",
                 "NIPPON INDIA SILVER ETF FOF",
                 "HDFC GOLD ETF FUND OF FUND"):
        nsdl = classify(section=Section.MUTUAL_FUND, isin="INF001", description=name)
        cams = classify(section=Section.UNKNOWN, isin="INF001", description=name)
        assert nsdl is cams, f"{name}: NSDL says {nsdl}, CAMS says {cams}"


def test_debt_funds_are_still_mutual_funds_in_the_mf_section():
    """Only gold/silver/PE may override the section context. Letting every
    keyword through would turn "HDFC Corporate Bond Fund" into a bond."""
    from app.classify import AssetClass, Section, classify
    for name in ("HDFC CORPORATE BOND FUND - DIRECT",
                 "SBI MAGNUM GILT FUND",
                 "ICICI PRU SHORT TERM BOND FUND",
                 "NIPPON INDIA ETF NIFTY BEES"):
        assert classify(section=Section.MUTUAL_FUND, isin="INF001",
                        description=name) is AssetClass.MUTUAL_FUND, name
