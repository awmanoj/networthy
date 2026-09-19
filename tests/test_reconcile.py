"""The CAS parser's self-check.

The most dangerous failure in this app isn't a crash — it's a wrong number shown
confidently. A CAS states its own portfolio total and the per-holding rows are
parsed separately, so the two are an independent check on each other. Both real
parser bugs found so far (a phantom holding read off a folio number, and an
equity section that came back entirely empty) would have been caught here.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage
from app.models import Account, Holding, ParsedStatement, Reconciliation


def _stmt(values, stated=1_000_000.0):
    holdings = [Holding(name=f"H{i}", asset_class="direct_equity", value=v)
                for i, v in enumerate(values)]
    accounts = [Account(kind="demat", name="DP", holdings=holdings)] if holdings else []
    return ParsedStatement(statement_date=date(2026, 8, 31), total_value=stated,
                           accounts=accounts)


# --- The check itself ---------------------------------------------------------

def test_exact_match_reconciles():
    r = _stmt([600_000, 400_000]).reconciliation
    assert r.ok and r.checked and r.delta == 0


def test_rounding_drift_is_tolerated():
    """A threshold tight enough to fire on ordinary statements is worse than
    none — a warning nobody believes is a warning nobody reads."""
    assert _stmt([600_000, 399_950]).reconciliation.ok


def test_a_missing_section_is_caught():
    """The real bug: an NSDL layout change made the whole equity block parse as
    empty, and the app showed an empty Equity leaf with no error at all."""
    r = _stmt([400_000]).reconciliation
    assert not r.ok
    assert r.delta < 0
    assert "less than" in r.summary()


def test_an_invented_value_is_caught():
    """The other real bug: a 12-digit folio number was read as a holding's value,
    producing a ₹477 billion position that nothing else would have flagged."""
    r = _stmt([600_000, 477_280_532_916]).reconciliation
    assert not r.ok
    assert r.delta > 0
    assert "more than" in r.summary()


def test_a_summary_cas_is_not_flagged():
    """A summary CAS carries a portfolio total and no per-holding rows at all.
    Flagging that would cry wolf on a perfectly good file."""
    r = _stmt([], stated=5_000_000.0).reconciliation
    assert r.ok and not r.checked


def test_zero_total_does_not_divide_by_zero():
    assert _stmt([0.0], stated=0.0).reconciliation.pct == 0.0


def test_summary_states_both_numbers_and_the_direction():
    r = _stmt([400_000]).reconciliation
    text = r.summary()
    assert "400,000" in text and "1,000,000" in text and "%" in text


# --- Surfaced to the user, not just computed ---------------------------------

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


def _login(email="rec@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _save(uid, stated, values):
    sid = storage.upsert_snapshot(uid, storage.Snapshot(
        statement_date=date(2026, 8, 31), total_value=stated,
        holding_count=len(values), source_filename="cas.pdf"))
    storage.replace_holdings(sid, _stmt(values, stated).accounts)
    return sid


def test_page_warns_when_stored_rows_do_not_add_up(client):
    uid, ck = _login()
    _save(uid, 1_000_000.0, [400_000])
    body = client.get("/nsdl-cas", cookies=ck).text
    assert "doesn't add up" in body
    # It must say the headline figure is still trustworthy, or the warning reads
    # as "your net worth is wrong" when only the breakdown is.
    assert "still correct" in body


def test_page_is_quiet_when_they_do(client):
    uid, ck = _login("quiet@test.com")
    _save(uid, 1_000_000.0, [600_000, 400_000])
    assert "doesn't add up" not in client.get("/nsdl-cas", cookies=ck).text


def test_the_warning_clears_when_a_good_statement_replaces_it(client):
    """A parser fix plus a re-upload has to clear this by itself — the check is
    recomputed from stored rows, never persisted as a flag that could go stale."""
    uid, ck = _login("fixed@test.com")
    _save(uid, 1_000_000.0, [400_000])
    assert "doesn't add up" in client.get("/nsdl-cas", cookies=ck).text
    _save(uid, 1_000_000.0, [600_000, 400_000])      # same date, re-uploaded
    assert "doesn't add up" not in client.get("/nsdl-cas", cookies=ck).text


def test_stored_reconciliation_matches_the_parse_time_one(client):
    """The page rebuilds the check from the database; it must agree with what the
    parser said at upload, or the two would tell the user different things."""
    uid, _ = _login("agree@test.com")
    values, stated = [400_000], 1_000_000.0
    sid = _save(uid, stated, values)
    accounts = storage.list_accounts(sid)
    stored = Reconciliation(
        stated=stated,
        parsed=sum(h.value or 0.0 for a in accounts for h in a.holdings),
        checked=bool(accounts))
    assert stored.summary() == _stmt(values, stated).reconciliation.summary()
