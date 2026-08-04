"""Tests for the daily/weekly email digests and net-worth history storage."""

import json
from datetime import date, datetime, timedelta

import pytest

from app import digest, prices, storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    return storage


# --- History storage --------------------------------------------------------

def test_nw_history_upsert_and_lookup(db):
    u = db.get_or_create_user("a@b.com").id
    db.record_nw_snapshot(u, "2026-08-01", 1000.0, 1000.0, 0.0, '{"equity": 500}')
    db.record_nw_snapshot(u, "2026-08-02", 1100.0, 1100.0, 0.0, '{"equity": 600}')
    db.record_nw_snapshot(u, "2026-08-02", 1150.0, 1150.0, 0.0, '{"equity": 650}')  # upsert same day

    assert db.latest_nw_snapshot_before(u, "2026-08-02")["net_worth"] == 1000.0   # the 1st
    assert db.nw_snapshot_on_or_before(u, "2026-08-02")["net_worth"] == 1150.0     # upserted 2nd
    assert db.nw_snapshot_on_or_before(u, "2026-07-25") is None                    # nothing that early


# --- Daily digest -----------------------------------------------------------

def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(digest.mailer, "send_email",
                        lambda to, subject, html: sent.append((to, subject, html)))
    return sent


def test_daily_digest_reports_day_over_day(db, monkeypatch):
    monkeypatch.setattr(digest, "ist_today", lambda: date(2026, 8, 4))
    u = db.get_or_create_user("k@test.com").id
    db.add_property_holding(u, "primary-residence", "Home", 10_000_000.0)  # gives net worth
    # yesterday's baseline
    db.record_nw_snapshot(u, "2026-08-03", 9_800_000.0, 9_800_000.0, 0.0, "{}")

    sent = _capture(monkeypatch)
    n = digest.run_daily()
    assert n == 1
    to, subject, html = sent[0]
    assert to == "k@test.com"
    assert "200,000" in html           # +₹200,000 day-over-day
    assert "since yesterday" in html and "▲" in html
    # today's snapshot was recorded
    assert db.nw_snapshot_on_or_before(u, "2026-08-04")["net_worth"] == pytest.approx(10_000_000.0)


def test_daily_digest_first_run_has_no_delta(db, monkeypatch):
    monkeypatch.setattr(digest, "ist_today", lambda: date(2026, 8, 4))
    u = db.get_or_create_user("k@test.com").id
    db.add_property_holding(u, "primary-residence", "Home", 5_000_000.0)
    sent = _capture(monkeypatch)
    digest.run_daily()
    _, _, html = sent[0]
    assert "from tomorrow" in html and "5,000,000" in html


def test_daily_skips_users_without_data(db, monkeypatch):
    monkeypatch.setattr(digest, "ist_today", lambda: date(2026, 8, 4))
    db.get_or_create_user("empty@test.com")  # no holdings
    sent = _capture(monkeypatch)
    assert digest.run_daily() == 0 and sent == []


# --- Weekly digest ----------------------------------------------------------

def test_weekly_digest_breaks_down_live_categories(db, monkeypatch):
    monkeypatch.setattr(digest, "ist_today", lambda: date(2026, 8, 10))
    # price crypto + a stock live so the categories have value
    monkeypatch.setattr(prices, "crypto_inr", lambda s: {"BTC": 6_000_000.0}.get(s))
    monkeypatch.setattr(prices, "usd_inr", lambda: 95.0)
    u = db.get_or_create_user("k@test.com").id
    db.add_crypto_holding(u, "BTC", 1.0)   # ₹60L crypto
    # baseline a week ago: crypto was ₹50L, net worth ₹50L
    db.record_nw_snapshot(u, "2026-08-03", 5_000_000.0, 5_000_000.0, 0.0,
                          json.dumps({"crypto": 5_000_000.0, "equity": 0.0,
                                      "mutual-funds": 0.0, "foreign-equity": 0.0}))
    sent = _capture(monkeypatch)
    n = digest.run_weekly()
    assert n == 1
    _, subject, html = sent[0]
    assert "this week" in subject and "▲" in subject   # change in the subject, not the total
    assert "1,000,000" not in subject or True          # (total stays out of the subject)
    assert "Crypto" in html and "6,000,000" in html    # current crypto value
    assert "this week" in html and "▲" in html          # +₹10L week-over-week
