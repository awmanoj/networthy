"""Networthy web app — upload NSDL CAS PDFs, track net worth over time."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, auth, expenses, networth, prices, storage, wealth
from .auth import SESSION_COOKIE, SessionMiddleware
from .classify import LABELS, AssetClass
from .parser import CASParseError, parse_cams, parse_cas


def _class_label(asset_class: str) -> str:
    """Human label for a stored asset-class value, tolerant of unknown values."""
    try:
        return LABELS[AssetClass(asset_class)]
    except ValueError:
        return asset_class or "Unclassified"

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Cache-bust token for static assets. Bound to process start, so every server
# restart (including --reload on edit, and every deploy) serves fresh CSS/JS.
templates.env.globals["version"] = str(int(time.time()))

app = FastAPI(title="Networthy", version=__version__)
app.add_middleware(SessionMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    storage.init_db()


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


# --- Auth -------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.state.user is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    auth.send_login_code(email)
    # Always land on the same screen regardless of send outcome.
    return RedirectResponse(url=f"/verify?email={email}", status_code=303)


@app.get("/verify", response_class=HTMLResponse)
def verify_form(request: Request, email: str = ""):
    if request.state.user is not None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "verify.html", {"request": request, "email": email, "error": None}
    )


@app.post("/verify", response_class=HTMLResponse)
def verify_submit(request: Request, email: str = Form(...), code: str = Form(...)):
    email = email.strip().lower()
    token = auth.verify_login_code(email, code.strip())
    if token is None:
        return templates.TemplateResponse(
            "verify.html",
            {
                "request": request,
                "email": email,
                "error": "Invalid or expired code. Please try again.",
            },
            status_code=400,
        )
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure(),
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --- App --------------------------------------------------------------------

@app.get("/nsdl-cas", response_class=HTMLResponse)
def nsdl_cas(request: Request):
    """NSDL CAS view — net-worth-over-time chart, the snapshots table, and the
    latest statement's detailed per-account holdings (the former Portfolio page)."""
    user = request.state.user
    snapshots = storage.list_snapshots(user.id)
    chart = [
        {"date": s.statement_date.isoformat(), "value": s.total_value}
        for s in snapshots
    ]
    latest = snapshots[-1] if snapshots else None
    change = None
    if len(snapshots) >= 2:
        change = snapshots[-1].total_value - snapshots[-2].total_value

    # Detailed holdings for the latest snapshot (merged in from the old Portfolio).
    accounts = storage.list_accounts(latest.id) if latest else []
    by_class: dict[str, float] = {}
    for account in accounts:
        for h in account.holdings:
            by_class[h.asset_class] = by_class.get(h.asset_class, 0.0) + (h.value or 0.0)
    total = sum(by_class.values())
    breakdown = [
        {
            "asset_class": ac,
            "label": _class_label(ac),
            "value": val,
            "pct": (val / total * 100) if total else 0.0,
        }
        for ac, val in sorted(by_class.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "snapshots": list(reversed(snapshots)),  # newest-first in the table
            "chart": chart,
            "latest": latest,
            "change": change,
            "accounts": accounts,
            "breakdown": breakdown,
            "class_label": _class_label,
        },
    )


@app.get("/portfolio")
def portfolio_redirect():
    """The Portfolio holdings moved onto the NSDL CAS page; keep the old URL."""
    return RedirectResponse(url="/nsdl-cas", status_code=307)


# Allocation colour per category slug — maps to the --c-* CSS tokens.
_CAT_COLOR = {
    "equity": "--c-equity", "mutual-funds": "--c-mf", "foreign-equity": "--c-us",
    "fixed-income": "--c-fixed", "gold-silver": "--c-gold", "bank-cash": "--c-bank",
    "foreign-exchange": "--c-forex", "alternate-investments": "--c-alt",
    "others": "--c-other", "real-estate": "--c-realty",
    "physical-gold": "--c-gold", "private-business": "--c-realty",
}
# The category level the dashboard summarises: the children of these parents.
_ALLOC_PARENTS = ("assets/financial-assets", "assets/non-financial-assets")


def _dashboard(user) -> dict:
    """Everything the home dashboard shows, derived from the rolled-up tree.

    Net worth = live Assets − Liabilities (sum-the-tree). Allocation buckets are the
    funded categories under Financial/Non-Financial assets, sorted by value.
    """
    values = _networth_values(user)
    assets = values.get("assets", 0.0)
    liabilities = values.get("liabilities", 0.0)

    buckets: list[dict] = []
    for parent in _ALLOC_PARENTS:
        chain = networth.resolve(parent)
        if not chain:
            continue
        for child in chain[-1].children:
            path = f"{parent}/{child.slug}"
            value = values.get(path)
            if not value:
                continue
            buckets.append({
                "label": child.title,
                "value": value,
                "color": _CAT_COLOR.get(child.slug, "--c-other"),
                "url": f"/networth/{path}",
            })
    buckets.sort(key=lambda b: b["value"], reverse=True)
    alloc_total = sum(b["value"] for b in buckets) or 1.0
    for b in buckets:
        b["pct"] = b["value"] / alloc_total * 100.0

    return {
        "net_worth": assets - liabilities,
        "assets": assets,
        "liabilities": liabilities,
        "fin_assets": values.get("assets/financial-assets", 0.0),
        "non_fin": values.get("assets/non-financial-assets", 0.0),
        "buckets": buckets,
        "has_data": bool(buckets) or liabilities > 0,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Dashboard (home) — the live net-worth summary: hero total, allocation,
    category tiles, funded holdings, and a CTA into the 'Where do you stand?' page."""
    user = request.state.user
    dash = _dashboard(user)
    return templates.TemplateResponse(
        "networth.html",
        {"request": request, "user": user, "dash": dash},
    )


@app.get("/networth")
def networth_home_redirect():
    """The tree overview lives on the dashboard now; keep the old URL working."""
    return RedirectResponse(url="/", status_code=307)


CAMS_IMPORT_URL = "/networth/import/cams"


@app.get(CAMS_IMPORT_URL, response_class=HTMLResponse)
def cams_import_form(request: Request):
    """How to generate a CAMS CAS + an upload form to import it."""
    return templates.TemplateResponse(
        "cams_import.html",
        {"request": request, "user": request.state.user, "error": None, "result": None},
    )


@app.post(CAMS_IMPORT_URL, response_class=HTMLResponse)
async def cams_import(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
):
    """Parse an uploaded CAMS CAS and store its holdings for the Networth pages.

    The password (usually the PAN) is used only to decrypt the PDF in memory — it
    is never stored. A prior CAMS import is replaced wholesale.
    """
    user = request.state.user
    try:
        contents = await file.read()
        parsed = parse_cams(contents, password or None)
    except CASParseError as exc:
        return templates.TemplateResponse(
            "cams_import.html",
            {"request": request, "user": user, "error": str(exc), "result": None},
            status_code=400,
        )

    storage.replace_networth_import(user.id, "cams", parsed.as_of_date, parsed.holdings)
    precious = sum(
        1 for h in parsed.holdings if h.asset_class in ("gold", "silver")
    )
    result = {
        "count": len(parsed.holdings),
        "mutual_funds": sum(1 for h in parsed.holdings if h.asset_class == "mutual_fund"),
        "precious": precious,
        "total": parsed.total_value,
        "as_of": parsed.as_of_date.strftime("%d %b %Y") if parsed.as_of_date else None,
    }
    return templates.TemplateResponse(
        "cams_import.html",
        {"request": request, "user": user, "error": None, "result": result},
    )


_IMPORT_CTA = {
    "cams": ("Import from CAMS", CAMS_IMPORT_URL),
    "nsdl": ("Upload NSDL CAS", "/upload"),
}


def _leaf_rows(user, slug: str) -> list[dict] | None:
    """Merged holding rows for a data-backed Networth leaf, or None if it isn't one.

    Precedence: for Mutual Funds, a CAMS import supersedes NSDL-classified MFs (same
    RTA feed — avoids double counting). For Gold & Silver the two sources are largely
    disjoint (funds vs demat SGB/ETF), so union them, deduped by ISIN with CAMS winning.
    Direct equity comes only from the NSDL CAS. No live enrichment here — callers add it.
    """
    classes = networth.LEAF_ASSET_CLASSES.get(slug)
    if not classes:
        return None

    cams = storage.list_networth_holdings(user.id, classes)
    nsdl = storage.latest_holdings_by_class(user.id, classes)
    if slug == "mutual-funds":
        return cams or nsdl
    seen = {h["isin"] for h in cams if h["isin"]}
    return cams + [h for h in nsdl if not h["isin"] or h["isin"] not in seen]


def _live_total(rows: list[dict]) -> float:
    """Sum of live values, falling back to the statement value for unpriced rows."""
    return sum(
        (r.get("live_value") if r.get("live_value") is not None else r["value"]) or 0.0
        for r in rows
    )


# The Foreign / US Equity leaf is its own thing: hand-entered tickers + shares,
# priced live in USD via Yahoo and converted to INR (no CAS, no fixed amount).
FOREIGN_LEAF = "foreign-equity"
# Foreign Exchange leaf: money held in a foreign currency (account or cash),
# valued live in INR at the currency's FX rate.
FOREX_LEAF = "foreign-exchange"


def _price_forex(rows: list[dict]) -> None:
    """Value foreign-currency holdings: amount × live FX rate → INR."""
    for h in rows:
        rate = prices.fx_to_inr(h["currency"])
        h["rate"] = rate
        h["value"] = (h["amount"] * rate) if rate is not None else None


# Alternate Investments leaf: illiquid, hand-valued bets (no live price).
ALT_LEAF = "alternate-investments"
# Physical Gold & Jewellery: each item is weight+karat (live-valued) or a flat value.
GOLD_LEAF = "physical-gold"
# Private Business: a hand-valued ownership stake.
BUSINESS_LEAF = "private-business"
_KARAT_FACTOR = {24: 1.0, 22: 0.916, 18: 0.75, 14: 0.585}


def _price_gold(rows: list[dict]) -> float | None:
    """Value gold items: a flat value if set, else weight × karat purity × live 24k
    rate. Returns the live 24k INR/gram rate used (None if unavailable)."""
    rate24 = prices.gold_inr_per_gram()
    for r in rows:
        flat = r.get("flat_value")
        if flat is not None:
            r["value"], r["rate"], r["basis"] = flat, None, "flat"
        elif r.get("weight_g") and r.get("karat") and rate24 is not None:
            r["rate"] = rate24 * _KARAT_FACTOR.get(int(r["karat"]), 1.0)
            r["value"] = r["weight_g"] * r["rate"]
            r["basis"] = "weight"
        else:
            r["value"], r["rate"], r["basis"] = None, None, None
    return rate24


def _property_share(row: dict) -> float:
    """A property's value attributed to the user, applying the ownership share
    (share_pct None = 100%). This is what rolls into net worth for joint property."""
    share = row.get("share_pct")
    share = 100.0 if share is None else share
    return (row.get("current_value") or 0.0) * share / 100.0


def _enrich_liability(rows: list[dict]) -> None:
    """Add display fields to liability rows: % paid off (from principal vs
    outstanding) and remaining tenure (from the end date)."""
    today = date.today()
    for r in rows:
        principal, out = r.get("principal"), r.get("outstanding")
        if principal and out is not None and principal > 0:
            r["paid_pct"] = max(0.0, (principal - out) / principal * 100.0)
        else:
            r["paid_pct"] = None
        end = _parse_date(r.get("end_date"))
        r["end_fmt"] = end.strftime("%b %Y") if end else None
        if not end:
            r["remaining"] = None
        elif end <= today:
            r["remaining"] = "ended"
        else:
            days = (end - today).days
            if days < 31:
                r["remaining"] = f"{days} d left"
            elif days < 365:
                r["remaining"] = f"{round(days / 30.44)} mo left"
            else:
                r["remaining"] = f"{days / 365.25:.1f} yrs left"


def _enrich_alt(rows: list[dict]) -> None:
    """Add display fields to alternate-investment rows: gain vs cost + a formatted
    invested date. `current_value` is the mark that counts toward net worth."""
    for r in rows:
        cost, cur = r.get("cost"), r.get("current_value")
        if cost and cur is not None:
            pct = (cur / cost - 1.0) * 100.0
            r["gain_pct"] = pct
            r["signal"] = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
        else:
            r["gain_pct"] = None
            r["signal"] = None
        inv = _parse_date(r.get("invested_date"))
        r["invested_fmt"] = inv.strftime("%b %Y") if inv else None


def _price_foreign(rows: list[dict]) -> float | None:
    """Price foreign holdings: live USD price × USD→INR into an INR value, plus
    gain% vs the optional cost. Returns the FX rate used (None if unavailable)."""
    fx = prices.usd_inr()
    for h in rows:
        usd = prices.get_quote(h["ticker"])
        h["price_usd"] = usd
        h["value"] = (h["units"] * usd * fx) if (usd is not None and fx) else None
        cost = h.get("cost_usd")
        if usd is not None and cost:
            pct = (usd / cost - 1.0) * 100.0
            h["gain_pct"] = pct
            h["signal"] = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
        else:
            h["gain_pct"] = None
            h["signal"] = None
    return fx


def _leaf_value(user, slug: str) -> float | None:
    """A leaf's live-consistent total — CAS holdings (live) + manual entries — or
    None if the leaf is neither data-backed nor manual-enabled. Rolls up the tree."""
    if slug == FOREIGN_LEAF:
        rows = storage.list_foreign_holdings(user.id)
        _price_foreign(rows)
        return sum(r["value"] or 0.0 for r in rows)

    if slug in networth.BANK_CASH_LEAVES:
        return sum(r["balance"] or 0.0 for r in storage.list_bank_cash(user.id, slug))

    if slug == FOREX_LEAF:
        rows = storage.list_forex_holdings(user.id)
        _price_forex(rows)
        return sum(r["value"] or 0.0 for r in rows)

    if slug == ALT_LEAF:
        return sum(r["current_value"] or 0.0 for r in storage.list_alt_investments(user.id))

    if slug in networth.REALTY_LEAVES:
        return sum(
            _property_share(r) for r in storage.list_property_holdings(user.id, slug)
        )

    if slug == GOLD_LEAF:
        rows = storage.list_gold_items(user.id)
        _price_gold(rows)
        return sum(r["value"] or 0.0 for r in rows)

    if slug == BUSINESS_LEAF:
        return sum(r["current_value"] or 0.0 for r in storage.list_business_holdings(user.id))

    if slug in networth.LIABILITY_LEAVES:
        return sum(r["outstanding"] or 0.0 for r in storage.list_liabilities(user.id, slug))

    data_backed = slug in networth.LEAF_ASSET_CLASSES
    manual_enabled = slug in networth.MANUAL_LEAVES
    if not (data_backed or manual_enabled):
        return None
    total = 0.0
    if data_backed:
        rows = _leaf_rows(user, slug) or []
        _annotate_live_prices(rows)
        total += _live_total(rows)
    if manual_enabled:
        total += sum(
            m["investment_amount"] for m in storage.list_manual_holdings(user.id, slug)
        )
    return total


def _networth_values(user) -> dict[str, float]:
    """Rolled-up value keyed by node slug-path, for every node that has data."""
    return networth.rollup(lambda slug: _leaf_value(user, slug))


def _leaf_holdings(user, slug: str) -> dict | None:
    """Everything a Networth leaf page needs: CAS holdings (live-priced) and/or
    hand-entered rows, or None if the leaf is neither data-backed nor manual."""
    if slug == FOREIGN_LEAF:
        rows = storage.list_foreign_holdings(user.id)
        fx = _price_foreign(rows)
        return {
            "is_foreign": True,
            "holdings": rows,
            "live_total": sum(r["value"] or 0.0 for r in rows),
            "fx": fx,
            "has_priced": any(r["value"] is not None for r in rows),
            "leaf_slug": slug,
        }

    if slug in networth.BANK_CASH_LEAVES:
        rows = storage.list_bank_cash(user.id, slug)
        return {
            "is_bank_cash": True,
            "is_bank": slug == "bank-accounts",
            "holdings": rows,
            "live_total": sum(r["balance"] or 0.0 for r in rows),
            "leaf_slug": slug,
        }

    if slug == FOREX_LEAF:
        rows = storage.list_forex_holdings(user.id)
        _price_forex(rows)
        return {
            "is_forex": True,
            "holdings": rows,
            "live_total": sum(r["value"] or 0.0 for r in rows),
            "has_priced": any(r["value"] is not None for r in rows),
            "leaf_slug": slug,
        }

    if slug == ALT_LEAF:
        rows = storage.list_alt_investments(user.id)
        _enrich_alt(rows)
        cost_total = sum(r["cost"] or 0.0 for r in rows if r.get("cost"))
        return {
            "is_alt": True,
            "holdings": rows,
            "live_total": sum(r["current_value"] or 0.0 for r in rows),
            "cost_total": cost_total,
            "leaf_slug": slug,
        }

    if slug in networth.REALTY_LEAVES:
        rows = storage.list_property_holdings(user.id, slug)
        _enrich_alt(rows)  # gain vs cost + date (both share-independent)
        for r in rows:
            r["share_pct"] = 100.0 if r.get("share_pct") is None else r["share_pct"]
            r["your_value"] = _property_share(r)
        has_joint = any(r["share_pct"] < 100 for r in rows)
        return {
            "is_property": True,
            "holdings": rows,
            "has_joint": has_joint,
            # Totals are attributed to the user's share (what rolls into net worth).
            "live_total": sum(r["your_value"] for r in rows),
            "cost_total": sum(
                (r["cost"] or 0.0) * r["share_pct"] / 100.0 for r in rows if r.get("cost")
            ),
            "leaf_slug": slug,
        }

    if slug == GOLD_LEAF:
        rows = storage.list_gold_items(user.id)
        rate24 = _price_gold(rows)
        return {
            "is_gold": True,
            "holdings": rows,
            "live_total": sum(r["value"] or 0.0 for r in rows),
            "rate24": rate24,
            "leaf_slug": slug,
        }

    if slug == BUSINESS_LEAF:
        rows = storage.list_business_holdings(user.id)
        _enrich_alt(rows)  # gain vs cost + date
        cost_total = sum(r["cost"] or 0.0 for r in rows if r.get("cost"))
        return {
            "is_business": True,
            "holdings": rows,
            "live_total": sum(r["current_value"] or 0.0 for r in rows),
            "cost_total": cost_total,
            "leaf_slug": slug,
        }

    if slug in networth.LIABILITY_LEAVES:
        rows = storage.list_liabilities(user.id, slug)
        _enrich_liability(rows)
        return {
            "is_liability": True,
            "holdings": rows,
            "live_total": sum(r["outstanding"] or 0.0 for r in rows),
            "emi_total": sum(r["emi"] or 0.0 for r in rows if r.get("emi")),
            "leaf_slug": slug,
        }

    data_backed = slug in networth.LEAF_ASSET_CLASSES
    manual_enabled = slug in networth.MANUAL_LEAVES
    if not (data_backed or manual_enabled):
        return None

    holdings = (_leaf_rows(user, slug) or []) if data_backed else []
    live_sources = _annotate_live_prices(holdings) if holdings else set()
    manual = storage.list_manual_holdings(user.id, slug) if manual_enabled else []
    _enrich_manual(manual)
    manual_total = sum(m["investment_amount"] for m in manual)

    sources = {h["source"] for h in holdings}
    source_label = {"cams": "CAMS statement", "nsdl": "NSDL CAS"}.get(
        next(iter(sources)) if len(sources) == 1 else "", "CAMS + NSDL CAS"
    ) if sources else None
    import_label, import_url = _IMPORT_CTA[networth.LEAF_IMPORT.get(slug, "cams")]
    as_of = next((h["as_of_date"] for h in holdings if h["as_of_date"]), None)
    return {
        "holdings": holdings,
        "has_cas": bool(holdings),
        "data_backed": data_backed,
        # Combined totals: CAS statement/live value + manual investment amounts.
        "total": sum(h["value"] or 0.0 for h in holdings) + manual_total,
        "live_total": _live_total(holdings) + manual_total,
        "has_live": bool(live_sources),
        "live_note": _LIVE_NOTES.get(
            frozenset(live_sources),
            "Live prices cached; only ticker symbols are sent externally.",
        ) if live_sources else None,
        "source": source_label,
        "as_of": as_of,
        "import_label": import_label,
        "import_url": import_url,
        # Manual entries.
        "manual_enabled": manual_enabled,
        "manual": manual,
        "manual_total": manual_total,
        "leaf_slug": slug,
    }


# Per-leaf provenance note, keyed by which live sources actually returned a price.
_LIVE_NOTES: dict[frozenset, str] = {
    frozenset({"yahoo"}):
        "Live prices from Yahoo Finance, cached ~15 min · only the ticker symbol "
        "is sent externally.",
    frozenset({"amfi"}):
        "Live NAVs from AMFI (public bulk feed), cached ~6 h · looked up locally, "
        "so nothing about your holdings is sent externally.",
    frozenset({"yahoo", "amfi"}):
        "Live equity prices from Yahoo Finance and fund NAVs from AMFI's public "
        "bulk feed, cached · only ticker symbols leave the machine.",
}


def _annotate_live_prices(holdings: list[dict]) -> set[str]:
    """Attach live_price / live_value / gain_pct / signal to each holding we can
    price. Returns the set of live sources used ("yahoo", "amfi").

    Resolution: an equity's exchange ticker -> Yahoo quote; any other ISIN-bearing
    holding (mutual funds, gold funds) -> AMFI NAV by ISIN. Rows we can't price keep
    None fields and the view falls back to their statement values.
    """
    quotes = prices.quotes_for_tickers([h.get("ticker") for h in holdings])
    # AMFI is only worth hitting for holdings without a ticker (i.e. funds).
    fund_isins = [h.get("isin") for h in holdings if not h.get("ticker")]
    navs = prices.navs_for_isins(fund_isins)

    used: set[str] = set()
    for h in holdings:
        h["live_price"] = None
        h["live_value"] = None
        h["gain_pct"] = None
        h["signal"] = None

        ticker = h.get("ticker")
        if ticker and ticker in quotes:
            live, src = quotes[ticker], "yahoo"
        elif h.get("isin") in navs:
            live, src = navs[h["isin"]], "amfi"
        else:
            continue

        used.add(src)
        h["live_price"] = live
        if h.get("units") is not None:
            h["live_value"] = h["units"] * live
        stmt_price = h.get("price")
        if stmt_price:
            pct = (live / stmt_price - 1.0) * 100.0
            h["gain_pct"] = pct
            h["signal"] = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
    return used


def _opt_float(raw: str) -> float | None:
    """Parse an optional numeric form field; blank or unparseable -> None."""
    try:
        return float(raw) if raw.strip() else None
    except (ValueError, AttributeError):
        return None


def _opt_date(raw: str) -> str | None:
    """Validate an optional ISO date form field; keep it as an ISO string or None."""
    raw = (raw or "").strip()
    try:
        return date.fromisoformat(raw).isoformat() if raw else None
    except ValueError:
        return None


def _parse_date(iso: str | None) -> date | None:
    try:
        return date.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def _years_label(years: float) -> str:
    """'15 yrs' when near-integer, else '1.5 yrs'."""
    if abs(years - round(years)) < 0.08:
        n = round(years)
        return f"{n} yr" if n == 1 else f"{n} yrs"
    return f"{years:.1f} yrs"


def _enrich_manual(rows: list[dict]) -> None:
    """Add display fields to manual rows: formatted dates, computed tenure, and a
    'matures in / matured' hint. Tenure comes from the two dates, falling back to a
    legacy `years` value for rows entered before dates existed."""
    today = date.today()
    for m in rows:
        inv = _parse_date(m.get("investment_date"))
        mat = _parse_date(m.get("maturity_date"))
        m["invested_fmt"] = inv.strftime("%d %b %Y") if inv else None
        m["matures_fmt"] = mat.strftime("%d %b %Y") if mat else None

        if inv and mat and mat > inv:
            m["tenure"] = _years_label((mat - inv).days / 365.25)
        elif m.get("years"):
            m["tenure"] = _years_label(m["years"])
        else:
            m["tenure"] = None

        m["matures_in"] = None
        if mat:
            if mat <= today:
                m["matures_in"] = "matured"
            else:
                days = (mat - today).days
                if days < 31:
                    m["matures_in"] = f"in {days} d"
                elif days < 365:
                    m["matures_in"] = f"in {round(days / 30.44)} mo"
                else:
                    m["matures_in"] = f"in {days / 365.25:.1f} yrs"


def _networth_redirect(path: str) -> RedirectResponse:
    """Redirect back to a Networth leaf, guarding against a bogus/injected path."""
    clean = path.strip("/")
    target = f"/networth/{clean}" if networth.resolve(clean) else "/networth"
    return RedirectResponse(url=target, status_code=303)


@app.post("/networth/manual/add")
def manual_add(
    request: Request,
    leaf_slug: str = Form(...),
    redirect: str = Form(...),
    scheme: str = Form(...),
    investment_amount: float = Form(...),
    maturity_amount: str = Form(""),
    investment_date: str = Form(""),
    maturity_date: str = Form(""),
    rate: str = Form(""),
):
    """Add a hand-entered holding to a manual-enabled Networth leaf."""
    if leaf_slug in networth.MANUAL_LEAVES and scheme.strip():
        storage.add_manual_holding(
            request.state.user.id,
            leaf_slug,
            scheme.strip(),
            investment_amount,
            maturity_amount=_opt_float(maturity_amount),
            rate=_opt_float(rate),
            investment_date=_opt_date(investment_date),
            maturity_date=_opt_date(maturity_date),
        )
    return _networth_redirect(redirect)


@app.post("/networth/manual/{holding_id}/delete")
def manual_delete(request: Request, holding_id: int, redirect: str = Form(...)):
    storage.delete_manual_holding(request.state.user.id, holding_id)
    return _networth_redirect(redirect)


@app.post("/networth/foreign/add")
def foreign_add(
    request: Request,
    redirect: str = Form(...),
    ticker: str = Form(...),
    units: float = Form(...),
    cost: str = Form(""),
):
    """Add a hand-entered foreign (US) equity holding: ticker + shares (+ cost)."""
    symbol = ticker.strip().upper()
    if symbol:
        storage.add_foreign_holding(request.state.user.id, symbol, units, _opt_float(cost))
    return _networth_redirect(redirect)


@app.post("/networth/foreign/{holding_id}/delete")
def foreign_delete(request: Request, holding_id: int, redirect: str = Form(...)):
    storage.delete_foreign_holding(request.state.user.id, holding_id)
    return _networth_redirect(redirect)


@app.post("/networth/forex/add")
def forex_add(
    request: Request,
    redirect: str = Form(...),
    currency: str = Form(...),
    amount: float = Form(...),
    kind: str = Form(""),
    label: str = Form(""),
):
    """Add a foreign-currency holding (amount in a currency, held account/cash)."""
    cur = currency.strip().upper()
    if cur:
        storage.add_forex_holding(
            request.state.user.id, cur, amount,
            kind=kind.strip() or None, label=label.strip() or None,
        )
    return _networth_redirect(redirect)


@app.post("/networth/forex/{holding_id}/delete")
def forex_delete(request: Request, holding_id: int, redirect: str = Form(...)):
    storage.delete_forex_holding(request.state.user.id, holding_id)
    return _networth_redirect(redirect)


@app.post("/networth/alt/add")
def alt_add(
    request: Request,
    redirect: str = Form(...),
    name: str = Form(...),
    current_value: float = Form(...),
    category: str = Form(""),
    cost: str = Form(""),
    invested_date: str = Form(""),
):
    """Add an alternate investment (illiquid, hand-valued)."""
    if name.strip():
        storage.add_alt_investment(
            request.state.user.id, name.strip(), current_value,
            category=category.strip() or None, cost=_opt_float(cost),
            invested_date=_opt_date(invested_date),
        )
    return _networth_redirect(redirect)


@app.post("/networth/alt/{inv_id}/delete")
def alt_delete(request: Request, inv_id: int, redirect: str = Form(...)):
    storage.delete_alt_investment(request.state.user.id, inv_id)
    return _networth_redirect(redirect)


@app.post("/networth/property/add")
def property_add(
    request: Request,
    leaf_slug: str = Form(...),
    redirect: str = Form(...),
    label: str = Form(...),
    current_value: float = Form(...),
    cost: str = Form(""),
    purchase_date: str = Form(""),
    notes: str = Form(""),
    share_pct: str = Form(""),
):
    """Add a property to a Real Estate sub-leaf."""
    if leaf_slug in networth.REALTY_LEAVES and label.strip():
        storage.add_property_holding(
            request.state.user.id, leaf_slug, label.strip(), current_value,
            cost=_opt_float(cost), purchase_date=_opt_date(purchase_date),
            notes=notes.strip() or None, share_pct=_opt_float(share_pct),
        )
    return _networth_redirect(redirect)


@app.post("/networth/property/{prop_id}/delete")
def property_delete(request: Request, prop_id: int, redirect: str = Form(...)):
    storage.delete_property_holding(request.state.user.id, prop_id)
    return _networth_redirect(redirect)


def _opt_int(raw: str) -> int | None:
    try:
        return int(raw) if raw.strip() else None
    except (ValueError, AttributeError):
        return None


@app.post("/networth/gold/add")
def gold_add(
    request: Request,
    redirect: str = Form(...),
    description: str = Form(...),
    weight_g: str = Form(""),
    karat: str = Form(""),
    flat_value: str = Form(""),
):
    """Add a physical-gold item (weight+karat for live valuation, or a flat value)."""
    desc = description.strip()
    weight, flat = _opt_float(weight_g), _opt_float(flat_value)
    # Need at least one basis to value it.
    if desc and (weight or flat is not None):
        storage.add_gold_item(
            request.state.user.id, desc,
            weight_g=weight, karat=_opt_int(karat), flat_value=flat,
        )
    return _networth_redirect(redirect)


@app.post("/networth/gold/{item_id}/delete")
def gold_delete(request: Request, item_id: int, redirect: str = Form(...)):
    storage.delete_gold_item(request.state.user.id, item_id)
    return _networth_redirect(redirect)


@app.post("/networth/business/add")
def business_add(
    request: Request,
    redirect: str = Form(...),
    name: str = Form(...),
    current_value: float = Form(...),
    ownership_pct: str = Form(""),
    cost: str = Form(""),
    invested_date: str = Form(""),
    notes: str = Form(""),
):
    """Add a private-business ownership stake."""
    if name.strip():
        storage.add_business_holding(
            request.state.user.id, name.strip(), current_value,
            ownership_pct=_opt_float(ownership_pct), cost=_opt_float(cost),
            invested_date=_opt_date(invested_date), notes=notes.strip() or None,
        )
    return _networth_redirect(redirect)


@app.post("/networth/business/{biz_id}/delete")
def business_delete(request: Request, biz_id: int, redirect: str = Form(...)):
    storage.delete_business_holding(request.state.user.id, biz_id)
    return _networth_redirect(redirect)


@app.post("/networth/liability/add")
def liability_add(
    request: Request,
    leaf_slug: str = Form(...),
    redirect: str = Form(...),
    lender: str = Form(...),
    outstanding: float = Form(...),
    principal: str = Form(""),
    rate: str = Form(""),
    emi: str = Form(""),
    end_date: str = Form(""),
    notes: str = Form(""),
):
    """Add a liability to a loan/dues leaf."""
    if leaf_slug in networth.LIABILITY_LEAVES and lender.strip():
        storage.add_liability(
            request.state.user.id, leaf_slug, lender.strip(), outstanding,
            principal=_opt_float(principal), rate=_opt_float(rate),
            emi=_opt_float(emi), end_date=_opt_date(end_date),
            notes=notes.strip() or None,
        )
    return _networth_redirect(redirect)


@app.post("/networth/liability/{liab_id}/delete")
def liability_delete(request: Request, liab_id: int, redirect: str = Form(...)):
    storage.delete_liability(request.state.user.id, liab_id)
    return _networth_redirect(redirect)


@app.post("/networth/bank/add")
def bank_cash_add(
    request: Request,
    leaf_slug: str = Form(...),
    redirect: str = Form(...),
    balance: float = Form(...),
    bank_name: str = Form(""),
    account_type: str = Form(""),
    label: str = Form(""),
):
    """Add a bank-account or cash entry to its leaf."""
    if leaf_slug in networth.BANK_CASH_LEAVES:
        storage.add_bank_cash(
            request.state.user.id,
            leaf_slug,
            balance,
            bank_name=bank_name.strip() or None,
            account_type=account_type.strip() or None,
            label=label.strip() or None,
        )
    return _networth_redirect(redirect)


@app.post("/networth/bank/{entry_id}/delete")
def bank_cash_delete(request: Request, entry_id: int, redirect: str = Form(...)):
    storage.delete_bank_cash(request.state.user.id, entry_id)
    return _networth_redirect(redirect)


@app.get("/networth/{path:path}", response_class=HTMLResponse)
def networth_node(request: Request, path: str):
    """A single category page.

    Categories list their children; data-backed leaves (Mutual Funds, Gold & Silver)
    render a holdings table; other leaves stay blank scaffolds.
    """
    if not path.strip("/"):
        return RedirectResponse(url="/", status_code=303)

    chain = networth.resolve(path)
    if chain is None:
        return templates.TemplateResponse(
            "networth_node.html",
            {
                "request": request,
                "user": request.state.user,
                "not_found": True,
                "breadcrumbs": networth.breadcrumbs([]),
            },
            status_code=404,
        )

    node = chain[-1]
    prefix = "/".join(n.slug for n in chain)
    values = _networth_values(request.state.user)
    children = [
        {
            "title": c.title,
            "note": c.note,
            "is_leaf": c.is_leaf,
            "child_count": len(c.children),
            "url": f"/networth/{prefix}/{c.slug}",
            "value": values.get(f"{prefix}/{c.slug}"),
        }
        for c in node.children
    ]
    leaf_data = _leaf_holdings(request.state.user, node.slug) if node.is_leaf else None
    return templates.TemplateResponse(
        "networth_node.html",
        {
            "request": request,
            "user": request.state.user,
            "not_found": False,
            "node": node,
            "children": children,
            "node_value": values.get(prefix),
            "node_path": prefix,
            "breadcrumbs": networth.breadcrumbs(chain),
            "leaf_data": leaf_data,
            "import_url": CAMS_IMPORT_URL,
        },
    )


@app.get("/standing", response_class=HTMLResponse)
def standing(request: Request):
    """The 'Where do you stand?' explorer — a full page reached from the dashboard
    CTA. Pre-fills with the user's live net worth (sum-the-tree) when they have data."""
    user = request.state.user
    dash = _dashboard(user)
    my_nw = dash["net_worth"] if dash["has_data"] else None
    default_nw = my_nw if my_nw else wealth.DEFAULT_NET_WORTH
    return templates.TemplateResponse(
        "standing.html",
        {
            "request": request,
            "user": user,
            "my_net_worth": my_nw,
            "default_net_worth": default_nw,
            "dataset": wealth.client_dataset(),
        },
    )


@app.get("/expenses", response_class=HTMLResponse)
def expenses_page(request: Request):
    """Recurring-expense planner: monthly/annual burn, category breakdown, and the
    net-worth connection (runway + FIRE target)."""
    user = request.state.user
    rows = storage.list_expenses(user.id)
    for r in rows:
        r["annual"] = expenses.annual_amount(r["amount"], r["count"], r["frequency"])
        r["monthly"] = r["annual"] / 12.0
        r["category_label"] = expenses.category_label(r["category"])
        r["category_color"] = expenses.category_color(r["category"])
        r["frequency_label"] = expenses.frequency_label(r["frequency"])

    annual_total = sum(r["annual"] for r in rows)
    monthly_total = annual_total / 12.0

    # Category breakdown (annual), largest first.
    by_cat: dict[str, float] = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + r["annual"]
    breakdown = [
        {
            "label": expenses.category_label(slug),
            "color": expenses.category_color(slug),
            "value": val,
            "pct": (val / annual_total * 100.0) if annual_total else 0.0,
        }
        for slug, val in sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
    ]

    # The net-worth connection: runway and a FIRE target.
    net_worth = _dashboard(user)["net_worth"]
    runway_years = (net_worth / annual_total) if annual_total > 0 else None
    fire_target = annual_total * expenses.FIRE_MULTIPLE if annual_total > 0 else None
    fire_pct = (net_worth / fire_target * 100.0) if fire_target else None

    return templates.TemplateResponse(
        "expenses.html",
        {
            "request": request,
            "user": user,
            "expenses": rows,
            "categories": expenses.CATEGORIES,
            "frequencies": expenses.FREQUENCIES,
            "monthly_total": monthly_total,
            "annual_total": annual_total,
            "breakdown": breakdown,
            "net_worth": net_worth,
            "runway_years": runway_years,
            "fire_target": fire_target,
            "fire_pct": fire_pct,
            "fire_multiple": expenses.FIRE_MULTIPLE,
        },
    )


@app.post("/expenses/add")
def expense_add(
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    amount: float = Form(...),
    frequency: str = Form(...),
    count: str = Form(""),
    notes: str = Form(""),
):
    """Add a recurring expense."""
    if (
        name.strip()
        and category in expenses.CATEGORY_BY_SLUG
        and frequency in expenses.FREQUENCIES
    ):
        storage.add_expense(
            request.state.user.id, name.strip(), category, amount, frequency,
            count=max(1, _opt_int(count) or 1), notes=notes.strip() or None,
        )
    return RedirectResponse(url="/expenses", status_code=303)


@app.post("/expenses/{expense_id}/delete")
def expense_delete(request: Request, expense_id: int):
    storage.delete_expense(request.state.user.id, expense_id)
    return RedirectResponse(url="/expenses", status_code=303)


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse(
        "upload.html", {"request": request, "user": request.state.user, "error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    password: str = Form(""),
):
    user = request.state.user
    # All CAS files for one person share the same password (the PAN), so a
    # single password applies to the whole batch. Each file is parsed
    # independently — one bad file doesn't sink the rest.
    results: list[dict] = []
    saved = 0
    for f in files:
        try:
            contents = await f.read()
            statement = parse_cas(
                contents, password or None, source_filename=f.filename
            )
        except CASParseError as exc:
            results.append({"filename": f.filename, "ok": False, "message": str(exc)})
            continue

        snapshot_id = storage.upsert_snapshot(
            user.id,
            storage.Snapshot(
                statement_date=statement.statement_date,
                total_value=statement.total_value,
                holding_count=statement.holding_count,
                source_filename=statement.source_filename,
            ),
        )
        # Store the detailed per-holding breakdown alongside the snapshot so the
        # portfolio view can explode it. Re-uploading a date rebuilds its rows.
        storage.replace_holdings(snapshot_id, statement.accounts)
        saved += 1
        results.append(
            {
                "filename": f.filename,
                "ok": True,
                "message": (
                    f"{statement.statement_date.strftime('%d %b %Y')} · "
                    f"₹{statement.total_value:,.0f} "
                    f"({statement.holding_count} holdings)"
                ),
            }
        )

    # 200 if at least one saved; 400 only if every file failed.
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "user": user,
            "error": None,
            "results": results,
            "saved": saved,
        },
        status_code=200 if saved else 400,
    )


@app.post("/snapshots/{snapshot_id}/delete")
def delete(request: Request, snapshot_id: int):
    storage.delete_snapshot(request.state.user.id, snapshot_id)
    return RedirectResponse(url="/nsdl-cas", status_code=303)


@app.post("/snapshots/delete-all")
def delete_all(request: Request):
    storage.delete_all_snapshots(request.state.user.id)
    return RedirectResponse(url="/nsdl-cas", status_code=303)
