"""The bug-report form: what it stores, what it sends, and what it refuses to attach."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, feedback, mailer, prices, storage


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


@pytest.fixture
def outbox(monkeypatch):
    """Capture send_email calls instead of letting anything reach the network."""
    sent = []

    def fake(to, subject, html, reply_to=None):
        sent.append({"to": to, "subject": subject, "html": html, "reply_to": reply_to})
        return True

    monkeypatch.setattr(mailer, "send_email", fake)
    monkeypatch.setenv("FEEDBACK_TO", "owner@example.com")
    return sent


def _login(email="reporter@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


MSG = "The equity leaf totals to zero after I upload a statement."


# --- Reachability ------------------------------------------------------------

def test_form_is_public(client):
    """A bug in a public calculator is reported by someone with no account."""
    r = client.get("/feedback", follow_redirects=False)
    assert r.status_code == 200
    assert "Report a bug" in r.text


def test_anonymous_can_submit(client, outbox):
    r = client.post("/feedback",
                    data={"kind": "bug", "message": MSG, "reply_to": "a@b.com"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "thank you" in r.text.lower()

    rows = storage.list_feedback()
    assert len(rows) == 1
    assert rows[0]["message"] == MSG
    assert rows[0]["user_id"] is None
    assert rows[0]["reply_to"] == "a@b.com"


# --- What is and isn't attached ----------------------------------------------

def test_signed_in_report_carries_the_account_and_nothing_else(client, outbox):
    uid, ck = _login()
    client.post("/feedback", data={"kind": "wrong-number", "message": MSG},
                cookies=ck, headers={"referer": "/networth/assets/financial-assets/crypto"})

    rows = storage.list_feedback()
    assert rows[0]["user_id"] == uid

    (mail,) = outbox
    assert mail["to"] == "owner@example.com"
    assert "reporter@test.com" in mail["html"]
    assert mail["reply_to"] == "reporter@test.com"   # replying reaches the reporter
    # The load-bearing assertion: the referring URL names which asset classes
    # someone holds, and it must not ride along. Same reason analytics is gated
    # off authenticated pages.
    assert "crypto" not in mail["html"]
    assert "/networth" not in mail["html"]


def test_message_is_escaped_into_the_email(outbox):
    feedback.submit("bug", "<script>alert(1)</script> & co", "", None, None)
    (mail,) = outbox
    assert "<script>" not in mail["html"]
    assert "&lt;script&gt;" in mail["html"]
    assert "&amp; co" in mail["html"]


# --- Validation and abuse ----------------------------------------------------

@pytest.mark.parametrize("message", ["", "   ", "broken"])
def test_too_short_is_rejected_and_keeps_what_was_typed(client, outbox, message):
    r = client.post("/feedback", data={"kind": "bug", "message": message,
                                       "reply_to": "keep@me.com"})
    assert r.status_code == 400
    assert "sentence or two" in r.text
    assert "keep@me.com" in r.text            # the form comes back filled in
    assert storage.list_feedback() == []
    assert outbox == []


def test_bad_reply_address_is_rejected(client, outbox):
    r = client.post("/feedback", data={"message": MSG, "reply_to": "not-an-email"})
    assert r.status_code == 400
    assert "email address" in r.text
    assert storage.list_feedback() == []


def test_unknown_kind_falls_back_rather_than_erroring(client, outbox):
    client.post("/feedback", data={"kind": "../etc/passwd", "message": MSG})
    assert storage.list_feedback()[0]["kind"] == feedback.DEFAULT_KIND


def test_message_is_capped(client, outbox):
    client.post("/feedback", data={"message": "x" * (feedback.MAX_MESSAGE + 500)})
    assert len(storage.list_feedback()[0]["message"]) == feedback.MAX_MESSAGE


def test_second_submission_is_throttled(client, outbox):
    _, ck = _login("spam@test.com")
    first = client.post("/feedback", data={"message": MSG}, cookies=ck)
    assert first.status_code == 200
    second = client.post("/feedback", data={"message": MSG + " again"}, cookies=ck)
    assert second.status_code == 429
    assert "give it a minute" in second.text
    assert len(storage.list_feedback()) == 1        # the second was not recorded


def test_anonymous_throttle_is_per_client_address(client, outbox):
    a = {"x-forwarded-for": "1.1.1.1, 10.0.0.1"}
    b = {"x-forwarded-for": "2.2.2.2"}
    assert client.post("/feedback", data={"message": MSG}, headers=a).status_code == 200
    assert client.post("/feedback", data={"message": MSG}, headers=a).status_code == 429
    # A different visitor is not blocked by someone else's submission.
    assert client.post("/feedback", data={"message": MSG}, headers=b).status_code == 200


# --- Delivery is best-effort, storage isn't ----------------------------------

def test_report_is_kept_even_with_no_destination_configured(monkeypatch, client):
    monkeypatch.delenv("FEEDBACK_TO", raising=False)
    monkeypatch.setenv("OWNER_EMAIL", "")
    r = client.post("/feedback", data={"message": MSG})
    assert r.status_code == 200
    assert storage.list_feedback()[0]["message"] == MSG


def test_report_is_kept_when_the_provider_fails(client, monkeypatch):
    monkeypatch.setenv("FEEDBACK_TO", "owner@example.com")
    monkeypatch.setattr(mailer, "send_email",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        feedback.submit("bug", MSG, "", None, None)
    assert storage.list_feedback()[0]["message"] == MSG   # written before the send


def test_destination_falls_back_to_the_owner(monkeypatch):
    monkeypatch.delenv("FEEDBACK_TO", raising=False)
    monkeypatch.setenv("OWNER_EMAIL", "me@example.com")
    assert feedback.destination() == "me@example.com"
    monkeypatch.setenv("FEEDBACK_TO", "issues@example.com")
    assert feedback.destination() == "issues@example.com"


def test_support_address_is_absent_unless_configured(monkeypatch, client):
    monkeypatch.delenv("SUPPORT_EMAIL", raising=False)
    assert "mailto:" not in client.get("/feedback").text     # no dead address
    monkeypatch.setenv("SUPPORT_EMAIL", "issues@networthyhq.com")
    assert "mailto:issues@networthyhq.com" in client.get("/feedback").text


# --- It's the user's data ----------------------------------------------------

def test_reports_are_exported_and_deleted_with_the_account(client, outbox):
    from app import exporter
    uid, ck = _login("mine@test.com")
    client.post("/feedback", data={"message": MSG}, cookies=ck)

    assert exporter.collect(uid)["feedback"][0]["message"] == MSG
    exporter.delete_everything(uid)
    assert exporter.collect(uid)["feedback"] == []


def test_thank_you_does_not_claim_delivery_that_did_not_happen(client, monkeypatch):
    """A self-hosted instance with no mail provider records the report and sends
    nothing. Telling that user it was emailed would be a lie."""
    monkeypatch.delenv("FEEDBACK_TO", raising=False)
    monkeypatch.setenv("OWNER_EMAIL", "")
    body = client.post("/feedback", data={"message": MSG}).text
    assert "saved on this server" in body
    assert "isn't emailed anywhere" in body          # the note agrees with the outcome
    assert "recorded and emailed" not in body
