"""Networthy web app — upload NSDL CAS PDFs, track net worth over time."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, storage
from .parser import CASParseError, parse_cas

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Cache-bust token for static assets. Bound to process start, so every server
# restart (including --reload on edit, and every deploy) serves fresh CSS/JS.
templates.env.globals["version"] = str(int(time.time()))

app = FastAPI(title="Networthy", version=__version__)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    storage.init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    snapshots = storage.list_snapshots()
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
            "snapshots": list(reversed(snapshots)),  # newest-first in the table
            "chart": chart,
            "latest": latest,
            "change": change,
        },
    )


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse(
        "upload.html", {"request": request, "error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(
    request: Request,
    files: list[UploadFile] = File(...),
    password: str = Form(""),
):
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

        storage.upsert_snapshot(
            storage.Snapshot(
                statement_date=statement.statement_date,
                total_value=statement.total_value,
                holding_count=statement.holding_count,
                source_filename=statement.source_filename,
            )
        )
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
        {"request": request, "error": None, "results": results, "saved": saved},
        status_code=200 if saved else 400,
    )


@app.post("/snapshots/{snapshot_id}/delete")
def delete(snapshot_id: int):
    storage.delete_snapshot(snapshot_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/snapshots/delete-all")
def delete_all():
    storage.delete_all_snapshots()
    return RedirectResponse(url="/", status_code=303)
