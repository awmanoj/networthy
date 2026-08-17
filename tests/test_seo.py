"""The public, indexable surface: the ranking page at /how-rich-am-i, its 301 from
the old /standing URL, per-page meta, and robots.txt / sitemap.xml.

The point of these is that a crawler is an *anonymous* client with no JavaScript:
if the page ever slips back behind the session gate, or its numbers go back to
being JS-only, it stops being indexable and these fail.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, main, prices, storage


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


def _login():
    uid = storage.get_or_create_user("k@test.com").id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return {auth.SESSION_COOKIE: "tok"}


# --- Reachability -----------------------------------------------------------

def test_ranking_page_is_public(client):
    r = client.get("/how-rich-am-i", follow_redirects=False)
    assert r.status_code == 200                       # not 303 -> /login
    assert "location" not in {k.lower() for k in r.headers}


def test_old_standing_url_301s_to_the_new_one(client):
    r = client.get("/standing", follow_redirects=False)
    assert r.status_code == 301                       # permanent: signals carry over
    assert r.headers["location"] == "/how-rich-am-i"


def test_app_routes_are_still_gated(client):
    # Making one page public must not open the rest of the app.
    for path in ("/networth", "/expenses", "/goals", "/nsdl-cas"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


# --- Indexable content ------------------------------------------------------

def test_threshold_table_answers_the_query_as_people_type_it(client):
    """"What net worth puts me in the top 1%" is the head query — the answer has
    to be in the HTML as prose, before the table, or it can't be lifted as a
    snippet."""
    page = client.get("/how-rich-am-i").text
    assert "What net worth puts you in the top 1% in India?" in page
    assert "₹1.92 crore" in page                      # top 1%, from wealth_for_top_pct
    assert "₹18.5 lakh" in page                       # top 10%
    assert "₹9.13 crore" in page                      # top 0.1%
    assert "top 0.01%" in page
    # Sub-₹1cr rows are interpolated where the source has no detail — footnoted.
    assert "stand-fn" in page and "no sub-band detail" in page


def test_numbers_are_server_rendered_not_js_only(client):
    """A crawler that never runs standing.js must still see real placements."""
    page = client.get("/how-rich-am-i").text
    assert "top 1% in India" in page
    for label in ("₹1 crore", "₹5 crore", "₹100 crore"):
        assert label in page
    # ₹1 crore ranks ~2.64% / 1 in 38 adults in India (see wealth.place_one).
    assert "2.64%" in page and "1 in 38" in page
    assert "India's wealth bands" in page


def test_tiny_band_shares_read_as_a_ratio_not_scientific_notation(client):
    # 205 billionaires among ~1bn adults is 2.05e-05% — useless as a percentage.
    rows = {r["label"]: r["share_display"] for r in main._standing_bands("india")}
    assert rows["> ₹10,000 cr"] == "1 in 4,878,049"
    assert rows["< ₹1 cr"] == "97.4%"
    assert not any("e-" in v for v in rows.values())


def test_public_page_states_its_sources_and_limits(client):
    """A public page about money must show its provenance in context — a stranger
    who lands here from a search will never read /terms."""
    page = client.get("/how-rich-am-i").text
    assert "Where these numbers come from" in page
    for source in ("UBS Global Wealth Report", "Knight Frank", "Forbes"):
        assert source in page
    assert "₹96.5 to $1" in page                       # the FX rate behind USD bands
    assert "piecewise power law" in page               # the interpolation method
    assert "not a census" in page
    assert "isn't financial advice" in page and 'href="/terms"' in page


def test_projection_pages_carry_the_disclaimer(client):
    ck = _login()
    storage.add_expense(storage.get_or_create_user("k@test.com").id,
                        "Rent", "housing", 100000.0, "monthly")
    for path in ("/expenses", "/goals"):
        page = client.get(path, cookies=ck).text
        assert "isn't financial advice" in page, path
        assert 'class="fine-print' in page, path


def test_footer_disclaimer_is_on_every_page(client):
    for path in ("/", "/how-rich-am-i", "/about", "/terms"):
        assert "indicative estimates, not financial advice" in client.get(path).text, path


def test_per_page_meta(client):
    page = client.get("/how-rich-am-i").text
    assert "<title>How rich am I? Net worth percentile for India · Networthy HQ</title>" in page
    assert '<link rel="canonical" href="https://networthyhq.com/how-rich-am-i" />' in page
    assert 'name="description" content="See where your net worth ranks.' in page
    assert 'property="og:url" content="https://networthyhq.com/how-rich-am-i"' in page
    # Exactly one description tag — the include owns it now, not marketing_base.
    assert page.count('name="description"') == 1


def test_landing_keeps_the_default_meta_and_only_one_description(client):
    page = client.get("/").text
    assert page.count('name="description"') == 1
    assert 'href="https://networthyhq.com/"' in page          # canonical
    assert "Track your complete net worth" in page


def test_logged_out_gets_signup_cta_logged_in_gets_breadcrumbs(client):
    anon = client.get("/how-rich-am-i").text
    assert "Get started" in anon and "Explore the live demo" in anon

    page = client.get("/how-rich-am-i", cookies=_login()).text
    assert "Breadcrumb" in page and "Dashboard" in page
    assert "Know your real number first" not in page   # CTA is for visitors only


# --- Crawler files ----------------------------------------------------------

def test_robots_txt(client):
    r = client.get("/robots.txt", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Allow: /how-rich-am-i" in r.text
    assert "Disallow: /networth" in r.text
    assert "Sitemap: https://networthyhq.com/sitemap.xml" in r.text


def test_sitemap_xml(client):
    r = client.get("/sitemap.xml", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<loc>https://networthyhq.com/how-rich-am-i</loc>" in r.text
    assert "<loc>https://networthyhq.com/</loc>" in r.text
    # Gated pages must never be advertised for crawling.
    for gated in ("/networth", "/expenses", "/goals", "/admin", "/login"):
        assert f"<loc>https://networthyhq.com{gated}</loc>" not in r.text


def test_sitemap_only_lists_public_paths(client):
    """Every URL in the sitemap must actually be crawlable by an anonymous client."""
    for path, _ in main._SITEMAP_PATHS:
        assert auth._is_public(path), f"{path} is in the sitemap but gated"
