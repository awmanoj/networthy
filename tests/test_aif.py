"""AIF / VC / PE units that arrive through a statement.

SEBI mandated demat for AIF units, so an angel, venture or private-equity
commitment shows up in an NSDL CAS next to the liquid holdings — carrying an ISIN
that looks exactly like a mutual fund's (INF) or a share's (INE). Only the name
separates them, and getting it wrong is worse than cosmetic: the commitment lands
in Mutual Funds looking redeemable, and if the holder also recorded it by hand
under Alternate Investments it counts twice in net worth, from two leaves they'd
never see side by side.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage
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


def _login(email="aif@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _cas_with_aif(uid):
    """A snapshot holding one AIF unit and one ordinary mutual fund."""
    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=3_000_000.0,
        holding_count=2, source_filename="cas.pdf",
    ))
    storage.replace_holdings(sid, [Account(kind="demat", name="NSDL", holdings=[
        Holding("ABC INDIA GROWTH FUND AIF CATEGORY II", "private_equity",
                "INF1234567890", 1000.0, 2000.0, 2_000_000.0),
        Holding("HDFC FLEXI CAP FUND - DIRECT GROWTH", "mutual_fund",
                "INF9876543210", 500.0, 2000.0, 1_000_000.0),
    ])])


def test_aif_from_a_cas_lands_in_alternate_investments_not_mutual_funds(client):
    """The point of the classification fix: an illiquid AIF commitment must not
    sit in the Mutual Funds leaf looking like something you can redeem."""
    uid, ck = _login()
    _cas_with_aif(uid)
    import app.main as m

    class U:  # the shape _leaf_value expects
        id = uid

    alt = m._leaf_value(U, m.ALT_LEAF)
    assert alt == 2_000_000.0, "AIF units missing from Alternate Investments"

    mf = m._leaf_value(U, "mutual-funds")
    assert mf == 1_000_000.0, "the AIF leaked into Mutual Funds"


def test_the_aif_shows_on_the_leaf_page_with_a_duplicate_warning(client):
    uid, ck = _login()
    _cas_with_aif(uid)
    page = client.get("/networth/assets/financial-assets/alternate-investments",
                      cookies=ck).text
    assert "ABC INDIA GROWTH FUND" in page
    assert "From your statements" in page
    # The warning is the point: someone who also entered this by hand needs to
    # see that it would count twice.
    assert "counts twice" in page


def test_hand_entered_and_cas_alt_investments_are_summed_not_replaced(client):
    uid, ck = _login()
    _cas_with_aif(uid)
    storage.add_alt_investment(uid, name="Angel — Acme", current_value=500_000.0)
    import app.main as m

    class U:
        id = uid

    assert m._leaf_value(U, m.ALT_LEAF) == 2_500_000.0


# --- Re-filing rows classified before the rules knew about AIF ---------------
#
# asset_class is computed at parse time and stored, so fixing the classifier only
# reaches statements uploaded afterwards. Everything already in the database keeps
# its old answer, which left real AIF units sitting in Mutual Funds.

def _stored_as_parsed_before_the_fix(uid):
    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=0.0,
        holding_count=4, source_filename="old.pdf"))
    storage.replace_holdings(sid, [Account(kind="demat", name="NSDL", holdings=[
        Holding("ABC INDIA GROWTH FUND AIF CATEGORY II", "mutual_fund",
                "INF111", 100.0, 1.0, 2_000_000.0),
        Holding("BLUME VENTURES FUND IV UNITS", "direct_equity",
                "INE222", 10.0, 1.0, 500_000.0),
        Holding("HDFC FLEXI CAP FUND", "mutual_fund", "INF333", 100.0, 1.0, 1_000_000.0),
        Holding("NPS TIER I SCHEME E", "nps", "INF444", 100.0, 1.0, 900_000.0),
    ])])


def test_migration_moves_old_aif_rows_out_of_mutual_funds(client):
    uid, _ck = _login("migrate@test.com")
    _stored_as_parsed_before_the_fix(uid)
    import app.main as m

    class U:
        id = uid

    assert m._leaf_value(U, "mutual-funds") == 3_000_000.0      # AIF stuck in MF
    assert m._leaf_value(U, m.ALT_LEAF) == 0.0

    assert storage.reclassify_holdings() == 2
    assert m._leaf_value(U, "mutual-funds") == 1_000_000.0      # only the real fund
    assert m._leaf_value(U, m.ALT_LEAF) == 2_500_000.0          # AIF + VC


def test_migration_leaves_nps_alone(client):
    """The reason this isn't a blanket re-classify: NPS is classified from CAS
    section context, and the section isn't stored. Re-running the rules blind
    would see an INF prefix and turn NPS holdings into mutual funds."""
    uid, _ck = _login("nps@test.com")
    _stored_as_parsed_before_the_fix(uid)
    import app.main as m

    class U:
        id = uid

    storage.reclassify_holdings()
    assert m._leaf_value(U, "nps") == 900_000.0


def test_migration_is_idempotent(client):
    uid, _ck = _login("idem@test.com")
    _stored_as_parsed_before_the_fix(uid)
    assert storage.reclassify_holdings() == 2
    assert storage.reclassify_holdings() == 0
    assert storage.reclassify_holdings() == 0


def test_migration_covers_cams_imports_too(client):
    uid, _ck = _login("cams-aif@test.com")
    storage.replace_networth_import(uid, "cams", date(2026, 7, 31), [
        Holding("XYZ ALTERNATIVE INVESTMENT FUND - CLASS A", "mutual_fund",
                "INF555", 10.0, 1.0, 750_000.0),
    ])
    assert storage.reclassify_holdings() == 1
    import app.main as m

    class U:
        id = uid

    assert m._leaf_value(U, m.ALT_LEAF) == 750_000.0


# --- Manual re-filing --------------------------------------------------------
#
# The rules can't win this one on their own: most real AIF names carry no marker
# at all — "True North Fund VI", "IIFL Special Opportunities Fund Series 7" — and
# read exactly like mutual funds. So there has to be a way to correct it by hand.

def _unmarked_aif(uid):
    """An AIF whose name gives nothing away, as most real ones don't."""
    sid = storage.upsert_snapshot(uid, Snapshot(
        statement_date=date(2026, 6, 30), total_value=0.0,
        holding_count=1, source_filename="cas.pdf"))
    storage.replace_holdings(sid, [Account(kind="demat", name="NSDL", holdings=[
        Holding("TRUE NORTH FUND VI", "mutual_fund", "INF777", 100.0, 1.0, 5_000_000.0),
    ])])


def test_an_unmarked_aif_can_be_re_filed_by_hand(client):
    uid, ck = _login("refile@test.com")
    _unmarked_aif(uid)
    import app.main as m

    class U:
        id = uid

    assert m._leaf_value(U, "mutual-funds") == 5_000_000.0     # the rules miss it

    r = client.post("/networth/holding/reclassify", cookies=ck, follow_redirects=False,
                    data={"isin": "INF777", "name": "TRUE NORTH FUND VI",
                          "asset_class": "private_equity",
                          "redirect": "/networth/assets/financial-assets/mutual-funds"})
    assert r.status_code == 303

    assert m._leaf_value(U, "mutual-funds") == 0.0
    assert m._leaf_value(U, m.ALT_LEAF) == 5_000_000.0


def test_an_override_survives_re_uploading_the_statement(client):
    """Keyed by ISIN, not row id: re-uploading rebuilds the holdings table, and an
    id-keyed override would vanish exactly when the user has stopped watching."""
    uid, ck = _login("survive@test.com")
    _unmarked_aif(uid)
    client.post("/networth/holding/reclassify", cookies=ck, follow_redirects=False,
                data={"isin": "INF777", "name": "TRUE NORTH FUND VI",
                      "asset_class": "private_equity"})

    _unmarked_aif(uid)          # the same statement, uploaded again
    import app.main as m

    class U:
        id = uid

    assert m._leaf_value(U, m.ALT_LEAF) == 5_000_000.0
    assert m._leaf_value(U, "mutual-funds") == 0.0


def test_an_override_can_be_undone(client):
    uid, ck = _login("undo@test.com")
    _unmarked_aif(uid)
    import app.main as m

    class U:
        id = uid

    client.post("/networth/holding/reclassify", cookies=ck, follow_redirects=False,
                data={"isin": "INF777", "name": "TRUE NORTH FUND VI",
                      "asset_class": "private_equity"})
    assert m._leaf_value(U, m.ALT_LEAF) == 5_000_000.0

    client.post("/networth/holding/reclassify", cookies=ck, follow_redirects=False,
                data={"isin": "INF777", "name": "TRUE NORTH FUND VI",
                      "asset_class": "auto"})
    assert m._leaf_value(U, "mutual-funds") == 5_000_000.0


def test_overrides_are_per_user(client):
    uid, ck = _login("mine2@test.com")
    _unmarked_aif(uid)
    other = storage.get_or_create_user("other2@test.com").id
    _unmarked_aif(other)

    client.post("/networth/holding/reclassify", cookies=ck, follow_redirects=False,
                data={"isin": "INF777", "name": "TRUE NORTH FUND VI",
                      "asset_class": "private_equity"})
    import app.main as m

    class Other:
        id = other

    assert m._leaf_value(Other, "mutual-funds") == 5_000_000.0   # untouched


def test_a_bogus_target_class_is_ignored(client):
    uid, ck = _login("bogus@test.com")
    _unmarked_aif(uid)
    client.post("/networth/holding/reclassify", cookies=ck, follow_redirects=False,
                data={"isin": "INF777", "name": "TRUE NORTH FUND VI",
                      "asset_class": "../../etc/passwd"})
    assert storage.get_holding_overrides(uid) == {}
