"""Running Networthy as a personal app on your own machine.

Three things had to change for `uvx networthy` to work, and each is a way the
hosted deployment could regress: the data directory has to be relocatable, the
email-code login has to be bypassable locally, and the session cookie has to
survive plain http. The last two are security-relevant, so the tests here care
as much about local mode staying *off* by default as about it working.
"""

import importlib
import os
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, launcher, prices, storage


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    monkeypatch.setenv("NETWORTHY_LOCAL", "1")
    import app.main as m
    return TestClient(m.app)


# --- Blocker 1: the data directory has to be relocatable --------------------

def test_data_dir_follows_the_env_var(monkeypatch, tmp_path):
    """A pip-installed package can't write to site-packages, so the DB location
    must come from the environment."""
    monkeypatch.setenv("NETWORTHY_DATA_DIR", str(tmp_path / "elsewhere"))
    importlib.reload(storage)
    try:
        assert storage.DATA_DIR == tmp_path / "elsewhere"
        assert storage.DB_PATH == tmp_path / "elsewhere" / "networthy.db"
    finally:
        monkeypatch.delenv("NETWORTHY_DATA_DIR")
        importlib.reload(storage)


def test_data_dir_defaults_beside_the_repo(monkeypatch):
    monkeypatch.delenv("NETWORTHY_DATA_DIR", raising=False)
    importlib.reload(storage)
    assert storage.DATA_DIR.name == "data"


def test_user_data_dir_is_platform_appropriate():
    d = launcher.user_data_dir()
    assert d.is_absolute()
    assert "networthy" in str(d).lower()
    assert str(d).startswith(str(Path.home()))


# --- Blocker 2: local mode signs the single user in -------------------------

def test_local_mode_signs_you_in_without_an_email_code(local_client):
    """The whole point: no OTP, no inbox, no reading a code out of the console."""
    r = local_client.get("/networth", follow_redirects=False)
    assert r.status_code == 200                      # not 303 -> /login
    assert auth.SESSION_COOKIE in r.cookies          # session issued on the way out


def test_local_session_persists_across_requests(local_client):
    """The cookie must actually stick, or every request mints a new session."""
    local_client.get("/")
    before = storage.get_or_create_user(auth.local_email()).id
    for _ in range(3):
        assert local_client.get("/expenses").status_code == 200
    assert storage.get_or_create_user(auth.local_email()).id == before


def test_local_mode_is_off_unless_asked_for(monkeypatch):
    """The hosted deployment must never take this path."""
    monkeypatch.delenv("NETWORTHY_LOCAL", raising=False)
    assert auth.local_mode() is False
    for value in ("0", "", "no", "off"):
        monkeypatch.setenv("NETWORTHY_LOCAL", value)
        assert auth.local_mode() is False
    for value in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("NETWORTHY_LOCAL", value)
        assert auth.local_mode() is True


def test_hosted_mode_still_gates_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.delenv("NETWORTHY_LOCAL", raising=False)
    import app.main as m
    client = TestClient(m.app)
    for path in ("/networth", "/expenses", "/goals", "/plan"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


# --- Blocker 3: the cookie has to survive plain http ------------------------

def test_cookie_is_not_secure_locally_but_is_by_default(monkeypatch):
    """A Secure cookie is never sent over http://127.0.0.1, so local mode would
    re-login on every request and never actually hold a session."""
    monkeypatch.delenv("COOKIE_SECURE", raising=False)

    monkeypatch.setenv("NETWORTHY_LOCAL", "1")
    assert auth.cookie_secure() is False

    monkeypatch.delenv("NETWORTHY_LOCAL")
    assert auth.cookie_secure() is True

    # An explicit setting still wins in both directions.
    monkeypatch.setenv("NETWORTHY_LOCAL", "1")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert auth.cookie_secure() is True


# --- The launcher -----------------------------------------------------------

def test_free_port_prefers_8321_and_falls_back(monkeypatch):
    assert launcher.free_port() > 0
    # With the preferred port occupied it must hand back something else, not fail:
    # a crash on startup is a terrible first impression for a one-command install.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        got = launcher.free_port(busy)
        assert got != busy and got > 0


def test_launcher_sets_up_the_environment_before_serving(monkeypatch, tmp_path):
    """The launcher owns the env contract: data dir and local mode must both be
    set before uvicorn (and therefore storage) is imported."""
    seen = {}
    import uvicorn

    def fake_run(app_path, **kw):
        seen["app"] = app_path
        seen["port"] = kw["port"]
        seen["data_dir"] = os.environ["NETWORTHY_DATA_DIR"]
        seen["local"] = os.environ["NETWORTHY_LOCAL"]

    monkeypatch.setattr(uvicorn, "run", fake_run)

    # The launcher writes os.environ directly — it has to, because the app reads
    # those values in this same process. monkeypatch can't undo writes it didn't
    # make, and leaking NETWORTHY_LOCAL into later tests silently un-gates the
    # whole app, so snapshot and restore explicitly.
    keys = ("NETWORTHY_LOCAL", "NETWORTHY_DATA_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.pop("NETWORTHY_LOCAL", None)
    try:
        rc = launcher.main(["--data-dir", str(tmp_path / "d"), "--port", "8999",
                            "--no-browser"])
        assert rc == 0
        assert seen["app"] == "app.main:app"
        assert seen["port"] == 8999
        assert seen["data_dir"] == str(tmp_path / "d")
        assert seen["local"] == "1"
        assert (tmp_path / "d").is_dir()             # created for the user
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_local_auto_login_only_applies_to_this_machine(tmp_path, monkeypatch):
    """Defence in depth: if NETWORTHY_LOCAL ever leaks into a hosted deploy, a
    remote request must still be gated rather than silently authenticated."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setenv("NETWORTHY_LOCAL", "1")
    import app.main as m

    class Remote:
        host = "203.0.113.9"

    class Req:
        client = Remote()

    assert auth._is_loopback(Req()) is False
    for host in ("127.0.0.1", "::1", "testclient"):
        Remote.host = host
        assert auth._is_loopback(Req()) is True


# --- The privacy note on the empty dashboard --------------------------------

def test_local_install_says_the_data_stays_put(local_client):
    """The reason someone chose to run it themselves, confirmed on arrival —
    including the path, so it's checkable rather than a promise."""
    page = local_client.get("/").text
    assert "This stays on your computer" in page
    assert "No account, no server" in page
    # The live-price caveat is load-bearing: without it the claim is false.
    assert "ticker symbol" in page


def test_hosted_never_claims_the_data_is_local(tmp_path, monkeypatch):
    """networthyhq.com stores data on a server. Showing the local note there
    would be a straightforward lie, so it must be impossible."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    monkeypatch.delenv("NETWORTHY_LOCAL", raising=False)

    from datetime import datetime, timedelta
    uid = storage.get_or_create_user("hosted@test.com").id
    storage.create_session(uid, "hosted-tok", datetime.utcnow() + timedelta(hours=1))

    import app.main as m
    page = TestClient(m.app).get("/", cookies={auth.SESSION_COOKIE: "hosted-tok"}).text
    assert "This stays on your computer" not in page
    assert "Let's find your number" in page           # same empty state otherwise
