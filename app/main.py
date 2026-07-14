"""Networthy web app — upload NSDL CAS PDFs, track net worth over time."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, auth, storage
from .auth import SESSION_COOKIE, SessionMiddleware
from .classify import LABELS, AssetClass
from .parser import CASParseError, parse_cas


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
