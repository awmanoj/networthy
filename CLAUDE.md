# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Networthy parses **NSDL CAS** (Consolidated Account Statement) PDFs — password-protected
statements consolidating demat holdings + mutual fund folios — and tracks total net worth
over time. It's a single-user, local-first tool: statements and the parsed DB live under
`data/` (gitignored) and never leave the machine. Server-rendered FastAPI + Jinja2, no
frontend framework.

## Commands

```bash
# Dev server (auto-reload)
source .venv/bin/activate
uvicorn app.main:app --reload            # http://127.0.0.1:8000

# Tests  (use `python -m pytest` — plain `pytest` won't put the repo root on sys.path)
python -m pytest                                   # all
python -m pytest tests/test_parser.py              # one file
python -m pytest tests/test_parser.py::test_to_float_strips_indian_grouping   # one test

# Docker
docker build -t networthy .
DOCKERHUB_USER=<name> ./deploy.sh [tag]  # build + push to Docker Hub
DOCKERHUB_USER=<name> ./run.sh [tag]     # run on server, published on port 8321
```

There is no linter/formatter configured.

## Architecture

The core data flow is one pipeline, worth understanding before touching any piece:

```
upload PDF(s)  →  parse_cas()  →  Snapshot  →  SQLite (data/networthy.db)  →  dashboard chart
```

- **`app/main.py`** — FastAPI routes. `POST /upload` takes N files + one shared password
  (the PAN — all of a person's CAS PDFs use the same one) and parses each independently:
  one bad file doesn't sink the batch (200 if any saved, 400 only if all fail). Delete routes:
  per-row `POST /snapshots/{id}/delete` and `POST /snapshots/delete-all`.

- **`app/parser/nsdl_cas.py`** — the fragile core. `parse_cas()` = pikepdf decrypt →
  pdfplumber text extraction → regex to pull `statement_date` and `total_value`. Raises
  `CASParseError` on wrong password or unrecognizable layout. Holding-level extraction is a
  stub; only the total + a holding count are stored today. CAS layouts vary by issuer/period,
  so the extraction regexes are the thing most likely to need hardening against real statements.

- **`app/storage.py`** — SQLite persistence. `upsert_snapshot()` keys on `statement_date`, so
  **re-uploading a statement for the same date replaces the existing snapshot** rather than
  duplicating. `list_snapshots()` returns oldest-first (chart-ready); the dashboard reverses
  for the newest-first table.

- **`app/classify.py`** — a layered, config-driven asset-class rule engine (section context >
  ISIN prefix > description keywords > manual override). **Not yet wired into the parse/storage
  flow** — it's exercised by tests and staged for when holding-level breakdown lands. The
  deliberate trap it guards: corporate bonds/NCDs share the `INE` prefix with equity, so ISIN
  alone can't separate them — description keywords must.

- **`app/models.py`** — dataclasses shared across layers: `Holding`, `ParsedStatement` (parser
  output), `Snapshot` (stored row).

## Testing approach

Tests target the fragile logic directly, without needing a real password-protected PDF:
- `test_parser.py` calls the private `_`-prefixed extraction helpers (`_find_statement_date`,
  `_find_total_value`, `_to_float`) against representative text snippets.
- `test_classify.py` pins the classification contract so keyword-table tuning can't silently
  regress established rules (INF=MF, bond keywords beat the INE equity default, etc.).

If you rename or change the signature of a `_`-prefixed parser helper, the tests break by design.

## Deployment notes

- The container port is parameterized by the `APP_PORT` env var (default 8000); `run.sh` sets it
  to 8321 for the server. The single image runs on any port — don't hardcode ports in the Dockerfile.
- The SQLite DB persists in the `networthy_data` Docker volume mounted at `/app/data`.
- `deploy.sh` requires a prior `docker login` (or `DOCKERHUB_TOKEN`) and tags each image with
  both the given tag and the short git SHA.

## Conventions

- Amounts are INR, formatted with Indian digit grouping (`_to_float` strips lakh/crore commas
  like `12,34,567.89`).
- The privacy invariant is load-bearing: never add code paths that write statement contents or
  parsed financial data anywhere outside `data/`, and keep `data/` / `*.pdf` / `*.db` gitignored.
