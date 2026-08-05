"""Tests for the assisted CAMS import: per-user PAN/email settings, the personalised
auto-fill bookmarklet, and the PAN-as-password fallback on upload."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage
from app.parser.cams_cas import CamsImport


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


def _login(email="k@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


# --- Settings storage -------------------------------------------------------

def test_user_settings_upsert_and_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    a = storage.get_or_create_user("a@b.com").id
    b = storage.get_or_create_user("b@b.com").id

    assert storage.get_user_settings(a) == {"pan": "", "cams_email": ""}
    storage.save_user_settings(a, "ABCDE1234F", "reg@cams.com")
    storage.save_user_settings(a, "ZZZZZ9999Z", "reg2@cams.com")   # upsert, one row
    assert storage.get_user_settings(a) == {"pan": "ZZZZZ9999Z", "cams_email": "reg2@cams.com"}
    assert storage.get_user_settings(b) == {"pan": "", "cams_email": ""}  # untouched


# --- Routes -----------------------------------------------------------------

def test_settings_save_uppercases_pan_and_builds_bookmarklet(client):
    uid, ck = _login()
    r = client.post("/networth/settings/cams",
                    data={"pan": "abcde1234f", "cams_email": "reg@cams.com"},
                    cookies=ck, follow_redirects=False)
    assert r.status_code == 303
    assert storage.get_user_settings(uid) == {"pan": "ABCDE1234F", "cams_email": "reg@cams.com"}

    page = client.get("/networth/import/cams", cookies=ck).text
    assert "javascript:(function()" in page              # bookmarklet rendered
    assert "ABCDE1234F" in page and "reg@cams.com" in page  # personalised
    assert 'value="ABCDE1234F"' in page                  # upload password pre-filled


def test_no_bookmarklet_before_pan_saved(client):
    _uid, ck = _login()
    page = client.get("/networth/import/cams", cookies=ck).text
    assert "javascript:(function()" not in page
    assert "Save your PAN above" in page


def test_import_falls_back_to_saved_pan_when_password_blank(client, monkeypatch):
    uid, ck = _login()
    storage.save_user_settings(uid, "MYPAN1234Z", "")

    seen = {}

    def fake_parse(contents, password):
        seen["password"] = password
        return CamsImport(holdings=[], as_of_date=None, total_value=0.0)

    import app.main as m
    monkeypatch.setattr(m, "parse_cams", fake_parse)
    # holdings empty -> replace_networth_import stores nothing; the route still renders.
    monkeypatch.setattr(m.storage, "replace_networth_import", lambda *a, **k: None)

    client.post("/networth/import/cams", cookies=ck,
                files={"file": ("cas.pdf", b"%PDF-1.4", "application/pdf")},
                data={"password": ""})
    assert seen["password"] == "MYPAN1234Z"   # fell back to the saved PAN


def test_import_uses_typed_password_over_saved(client, monkeypatch):
    uid, ck = _login()
    storage.save_user_settings(uid, "SAVEDPAN1Z", "")
    seen = {}
    import app.main as m
    monkeypatch.setattr(m, "parse_cams",
                        lambda c, password: seen.update(password=password) or
                        CamsImport(holdings=[], as_of_date=None, total_value=0.0))
    monkeypatch.setattr(m.storage, "replace_networth_import", lambda *a, **k: None)
    client.post("/networth/import/cams", cookies=ck,
                files={"file": ("cas.pdf", b"%PDF-1.4", "application/pdf")},
                data={"password": "TYPED5678Z"})
    assert seen["password"] == "TYPED5678Z"   # explicit password wins
