"""Tests for the Dashboard net-worth trend: the nw_history accessors and the
dashboard-view bootstrap that records today's point."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, digest, prices, storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    return storage


def test_list_nw_history_is_oldest_first(db):
    u = db.get_or_create_user("a@b.com").id
    db.record_nw_snapshot(u, "2026-08-03", 1100.0, 1100.0, 0.0, "{}")
    db.record_nw_snapshot(u, "2026-08-01", 1000.0, 1000.0, 0.0, "{}")
    series = db.list_nw_history(u)
    assert [p["date"] for p in series] == ["2026-08-01", "2026-08-03"]
    assert [p["value"] for p in series] == [1000.0, 1100.0]


def test_ensure_nw_point_inserts_once_and_never_clobbers(db):
    u = db.get_or_create_user("a@b.com").id
    # A digest-written row with a real breakdown.
    db.record_nw_snapshot(u, "2026-08-05", 2000.0, 2000.0, 0.0, '{"equity": 500}')
    # A later dashboard view must NOT overwrite it (breakdown preserved, value kept).
    db.ensure_nw_point(u, "2026-08-05", 9999.0, 9999.0, 0.0)
    row = db.nw_snapshot_on_or_before(u, "2026-08-05")
    assert row["net_worth"] == 2000.0 and row["breakdown"] == '{"equity": 500}'
    # But it does create a point for a day that had none.
    db.ensure_nw_point(u, "2026-08-06", 2100.0, 2100.0, 0.0)
    assert db.nw_snapshot_on_or_before(u, "2026-08-06")["net_worth"] == 2100.0


# --- Dashboard bootstrap ----------------------------------------------------

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


def _login(uid):
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return {auth.SESSION_COOKIE: "tok"}


def test_dashboard_view_bootstraps_one_point_per_day(client):
    uid = storage.get_or_create_user("k@t.com").id
    storage.add_property_holding(uid, "primary-residence", "Home", 5_000_000.0)
    ck = _login(uid)
    client.get("/", cookies=ck)
    client.get("/", cookies=ck)  # same IST day -> still one point
    hist = storage.list_nw_history(uid)
    assert len(hist) == 1
    assert hist[0]["date"] == digest.ist_today().isoformat()
    assert hist[0]["value"] == pytest.approx(5_000_000.0)


def test_trend_chart_renders_only_with_two_or_more_points(client):
    uid = storage.get_or_create_user("k@t.com").id
    storage.add_property_holding(uid, "primary-residence", "Home", 5_000_000.0)
    ck = _login(uid)
    # One point (today) -> the "trend builds" note, no chart.
    page = client.get("/", cookies=ck).text
    assert "trend starts today" in page and 'id="trend-ranges"' not in page
    # Backfill a prior day -> the chart + range toggles appear.
    storage.record_nw_snapshot(uid, "2026-01-01", 4_000_000.0, 4_000_000.0, 0.0, "{}")
    page = client.get("/", cookies=ck).text
    assert 'id="trend-ranges"' in page and 'id="chart"' in page
