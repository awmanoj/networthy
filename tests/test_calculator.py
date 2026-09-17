"""The public net-worth calculator: its content, its sourcing, and what it won't leak."""

import html
import json
import pathlib
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, main, networth, prices, storage

PATH = "/net-worth-calculator"


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


def _login(email="calc@test.com"):
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return {auth.SESSION_COOKIE: "tok"}


# --- Reachable and indexable -------------------------------------------------

def test_page_is_public(client):
    r = client.get(PATH, follow_redirects=False)
    assert r.status_code == 200
    assert "location" not in {k.lower() for k in r.headers}


def test_it_is_in_the_sitemap_and_robots(client):
    assert PATH in client.get("/sitemap.xml").text
    assert f"Allow: {PATH}" in client.get("/robots.txt").text


def test_every_sitemap_path_is_public():
    """The invariant the rest of the SEO surface already keeps."""
    for path, _ in main._SITEMAP_PATHS:
        assert auth._is_public(path), path


# --- The content a crawler sees (it runs no JavaScript) ----------------------

def test_the_answer_is_server_rendered_not_js_only(client):
    body = client.get(PATH).text
    assert "Net worth = Assets − Liabilities" in body
    # The categories are the indexable substance; the JS only adds them up.
    assert "Mutual Funds" in body and "Home Loan" in body
    assert "EPF" in body                                  # the commonly forgotten one
    assert "Total assets" in body


def test_rows_come_from_the_real_tree(client):
    """Curated as slugs *into* networth.SECTIONS, so the page advertising the
    product can't drift from the product. A renamed or deleted node fails here."""
    slugs = set()

    def walk(n):
        slugs.add(n.slug)
        for c in n.children:
            walk(c)

    for section in networth.SECTIONS:
        walk(section)
    missing = set(main._CALC_ASSETS + main._CALC_LIABILITIES) - slugs
    assert not missing, f"calculator references nodes that no longer exist: {missing}"
    assert len(main._calc_rows(main._CALC_ASSETS)) == len(main._CALC_ASSETS)


def test_every_row_has_a_hint(client):
    """A calculator row without an example is where people guess wrong."""
    for r in main._calc_rows(main._CALC_ASSETS + main._CALC_LIABILITIES):
        assert r["hint"], r["slug"]


def test_faq_is_visible_and_matches_the_schema(client):
    """Google requires FAQ markup to match what a visitor can actually read. A
    second copy for the crawler would be a policy violation, not a shortcut."""
    raw = client.get(PATH).text
    # Jinja escapes apostrophes in the rendered <dd> but the JSON-LD carries them
    # literally, so the two only compare after unescaping.
    body = html.unescape(raw)
    (block,) = [json.loads(b) for b in
                re.findall(r'<script type="application/ld\+json">(.*?)</script>', raw, re.S)]
    faq = next(n for n in block["@graph"] if n["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) == len(main._CALC_FAQ)
    for item in faq["mainEntity"]:
        assert item["name"] in body                      # the question is on the page
        assert item["acceptedAnswer"]["text"] in body    # and so is the whole answer


# --- Privacy -----------------------------------------------------------------

def test_the_ranking_handoff_never_puts_money_in_a_url():
    """A ?nw= param would send the visitor's net worth to the server in the
    request line — into access logs, and on to Google Analytics, which runs on
    exactly these public pages. The handoff goes through sessionStorage."""
    src = pathlib.Path(__file__).parent.parent / "app" / "static"
    calc = (src / "calculator.js").read_text()
    # Comments stripped first: the file explains at length why a query string is
    # wrong, and that explanation must not be what satisfies the assertion.
    code = "\n".join(l for l in calc.splitlines() if not l.lstrip().startswith("//"))
    assert "nw-handoff" in code
    assert "?nw=" not in code and "nw=" not in code
    assert "fetch(" not in code and "XMLHttpRequest" not in code
    assert "nw-handoff" in (src / "standing.js").read_text()   # the other half


def test_signed_in_page_emits_no_schema_and_no_analytics(client):
    body = client.get(PATH, cookies=_login()).text
    assert "application/ld+json" not in body
    assert "googletagmanager" not in body


# --- Cross-linking -----------------------------------------------------------

def test_the_four_tools_link_to_each_other(client):
    for path in (PATH, "/how-rich-am-i", "/how-much-do-i-need-to-retire",
                 "/how-do-i-get-to-10-crore"):
        body = client.get(path).text
        others = {"/how-rich-am-i", "/how-much-do-i-need-to-retire",
                  "/how-do-i-get-to-10-crore", PATH} - {path}
        for other in others:
            assert f'href="{other}"' in body, f"{path} does not link {other}"


def test_input_parser_handles_how_people_actually_type_money():
    """Exercised through Node because the parser lives in the browser. People
    write "28 lakh" and "1.1cr"; rejecting that loses them on the first row.

    The `-5` case is the one that matters: stripping non-numerics ate the sign,
    so an overdraft typed into the bank row *added* to assets."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available")
    script = r"""
      const src = require("fs").readFileSync("app/static/calculator.js", "utf8");
      const body = src.slice(src.indexOf("const parse ="), src.indexOf("const sum ="));
      const parse = eval("(" + body.replace(/^\s*const parse =/, "").replace(/;\s*$/, "") + ")");
      const cases = [["", 0], ["42,00,000", 4200000], ["28 lakh", 2800000],
                     ["1.1cr", 11000000], ["50k", 50000], ["2.5 lac", 250000],
                     ["-5", 0], ["−5", 0], ["abc", 0]];
      for (const [input, want] of cases) {
        const got = parse(input);
        if (got !== want) { console.error(JSON.stringify(input), got, want); process.exit(1); }
      }
    """
    assert subprocess.run(["node", "-e", script]).returncode == 0
