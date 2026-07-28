"""Networthy web app — upload NSDL CAS PDFs, track net worth over time."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, auth, networth, prices, storage, wealth
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

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "snapshots": list(reversed(snapshots)),  # newest-first in the table
            "chart": chart,
            "latest": latest,
            "change": change,
        },
    )


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio(request: Request):
    """Detailed holdings view for the user's most recent statement.

    Always renders live from the latest snapshot's stored holdings, so uploading
    a newer detailed CAS updates it automatically; the Refresh button just
    re-renders (a future performance-signal pass will recompute here).
    """
    user = request.state.user
    latest = storage.latest_snapshot(user.id)
    accounts = storage.list_accounts(latest.id) if latest else []

    # Asset-class rollup across every account, for the coloured summary strip.
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
        "portfolio.html",
        {
            "request": request,
            "user": user,
            "latest": latest,
            "accounts": accounts,
            "breakdown": breakdown,
            "class_label": _class_label,
        },
    )


@app.get("/networth", response_class=HTMLResponse)
def networth_home(request: Request):
    """Overview of the Assets / Liabilities breakdown — the whole tree at a glance."""
    return templates.TemplateResponse(
        "networth.html",
        {
            "request": request,
            "user": request.state.user,
            "sections": networth.SECTIONS,
            "values": _networth_values(request.state.user),
        },
    )


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


def _leaf_value(user, slug: str) -> float | None:
    """A leaf's live-consistent total — CAS holdings (live) + manual entries — or
    None if the leaf is neither data-backed nor manual-enabled. Rolls up the tree."""
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


@app.get("/networth/{path:path}", response_class=HTMLResponse)
def networth_node(request: Request, path: str):
    """A single category page.

    Categories list their children; data-backed leaves (Mutual Funds, Gold & Silver)
    render a holdings table; other leaves stay blank scaffolds.
    """
    if not path.strip("/"):
        return RedirectResponse(url="/networth", status_code=303)

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
    """The "Where do you stand?" explorer — rank a net worth across geographies.

    Pre-fills with the user's own latest net worth if they have a snapshot, else a
    playful default, and hands the first placement to the template so the page has
    something on screen before the interactive JS takes over.
    """
    user = request.state.user
    latest = storage.latest_snapshot(user.id)
    default_nw = latest.total_value if latest else wealth.DEFAULT_NET_WORTH
    return templates.TemplateResponse(
        "standing.html",
        {
            "request": request,
            "user": user,
            "my_net_worth": latest.total_value if latest else None,
            "default_net_worth": default_nw,
            "dataset": wealth.client_dataset(),
        },
    )


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
    return RedirectResponse(url="/", status_code=303)


@app.post("/snapshots/delete-all")
def delete_all(request: Request):
    storage.delete_all_snapshots(request.state.user.id)
    return RedirectResponse(url="/", status_code=303)
