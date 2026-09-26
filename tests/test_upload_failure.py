"""What happens when a statement won't parse.

This is the highest-stakes screen in the app: it's the first thing a new user
does, and before this it showed a ✕, one sentence, and nothing else — no way
forward and no way to tell us. A statement we can't read is the single most
likely reason someone tries this once and never returns.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, prices, storage
from app.parser import CASParseError
from app.parser.diagnose import as_text, diagnose, mask


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


def _login(email="up@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


def _upload(client, ck, raiser):
    """Post a file, with parse_cas replaced by something that fails a given way."""
    import app.main as m
    original = m.parse_cas
    m.parse_cas = raiser
    try:
        return client.post("/upload",
                           files={"files": ("cas.pdf", b"%PDF-1.4 fake", "application/pdf")},
                           data={"password": "ABCDE1234F"}, cookies=ck).text
    finally:
        m.parse_cas = original


def _fails(cause, msg="nope"):
    def raiser(*a, **k):
        raise CASParseError(msg, cause=cause)
    return raiser


# --- The error carries a cause, not just prose -------------------------------

def test_causes_are_structured_not_string_matched():
    assert CASParseError("x", cause="password").cause == "password"
    assert CASParseError("x").cause == "unknown"      # a default, never a crash


# --- Every failure offers a way forward --------------------------------------

@pytest.mark.parametrize("cause,expect", [
    ("password", "PAN in capitals"),
    ("scanned", "a picture, not a document"),
    ("unreadable", "didn't open as a PDF"),
    ("layout", "our fault, not yours"),
])
def test_each_cause_gets_its_own_way_out(client, cause, expect):
    _, ck = _login()
    body = _upload(client, ck, _fails(cause))
    assert expect in body


def test_every_failure_says_a_statement_is_optional(client):
    """The escape hatch that makes a parse failure survivable: the CAS is a
    shortcut, and nothing on this page used to say so."""
    _, ck = _login("opt@test.com")
    for cause in ("password", "scanned", "unreadable", "layout", "unknown"):
        body = _upload(client, ck, _fails(cause))
        assert "add your holdings" in body and "not a requirement" in body, cause


def test_a_layout_failure_offers_a_one_click_report(client):
    _, ck = _login("rep@test.com")
    body = _upload(client, ck, _fails("layout"))
    assert 'action="/feedback"' in body
    assert "name=\"form_token\"" in body


def test_a_wrong_password_does_not_get_a_diagnostic(client):
    """No forensics needed on something the reader fixes by retyping it — and
    we couldn't decrypt the file to produce one anyway."""
    _, ck = _login("pw@test.com")
    body = _upload(client, ck, _fails("password"))
    assert "Send us the shape" not in body


# --- The diagnostic describes the layout and nothing in it -------------------

def test_masking_keeps_the_shape_and_destroys_the_content():
    line = "MANOJ AWASTHI INE002A01018 Reliance Industries 120.000 2,840.50 3,40,860.00"
    out = mask(line)
    # Punctuation and spacing survive because they *are* the diagnostic: the
    # difference between "9,99,999.99" and "999999.99" is what breaks a regex.
    assert out == "XXXXX XXXXXXX XXX999X99999 XXXXXXXX XXXXXXXXXX 999.999 9,999.99 9,99,999.99"
    for secret in ("MANOJ", "AWASTHI", "INE002A01018", "Reliance", "840", "860"):
        assert secret not in out


def test_the_report_never_raises_on_a_broken_file():
    """A diagnostic that dies on a bad file is useless exactly when it's needed."""
    report = diagnose(b"not a pdf at all", None)
    assert report["opened"] is False and report["error"]
    assert "could not read the file" in as_text(report)


def test_the_rendered_report_carries_no_raw_text(client):
    """Whatever ends up on screen must be masked or a count — never a line
    lifted out of someone's statement."""
    import app.parser.diagnose as d
    text = ("Consolidated Account Statement as on 31-AUG-2026\n"
            "MANOJ AWASTHI INE002A01018 Reliance Industries 120.000 2,840.50 3,40,860.00\n")
    monkey = lambda *a, **k: text
    original, d.extract_text = d.extract_text, monkey
    try:
        rendered = as_text(diagnose(b"x", None))
    finally:
        d.extract_text = original
    for secret in ("MANOJ", "AWASTHI", "INE002A01018", "Reliance", "2,840.50"):
        assert secret not in rendered
    assert "ISIN-bearing rows : 1" in rendered
    assert "NO MATCH" in rendered or "matched" in rendered
