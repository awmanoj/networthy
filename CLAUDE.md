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
upload PDF(s)  →  parse_cas()  →  Snapshot + Accounts/Holdings  →  SQLite  →  dashboard chart + /portfolio
```

- **`app/main.py`** — FastAPI routes. `POST /upload` takes N files + one shared password
  (the PAN — all of a person's CAS PDFs use the same one) and parses each independently:
  one bad file doesn't sink the batch (200 if any saved, 400 only if all fail). It stores both
  the `Snapshot` and its detailed per-holding rows (`replace_holdings`). Delete routes:
  per-row `POST /snapshots/{id}/delete` and `POST /snapshots/delete-all`. `GET /portfolio`
  renders the latest snapshot's holdings grouped by account (colour-coded by asset class);
  its Refresh button just re-renders — a future performance-signal pass will recompute there.

- **`app/parser/nsdl_cas.py`** — the fragile core. `parse_cas()` = pikepdf decrypt →
  pdfplumber text extraction → regex to pull `statement_date` and `total_value`, plus
  `_find_accounts()` for the detailed breakdown. Raises `CASParseError` on wrong password or
  unrecognizable layout. `_find_accounts` is section-aware and **ISIN-anchored**: it walks text
  lines, tracks the current section/account, treats any ISIN-bearing line as a holding. The
  name is the words before the **trailing run of numbers** (so digits inside names — the "2"
  in "E2E", the "50" in "Nifty 50" — survive), and those trailing numbers are read positionally
  as (units, price, value). Wrapped name lines are stitched back on (`_is_name_tail`, skipping
  ticker lines like "E2E.NSE"); prose ISIN mentions and nil/0-value rows are dropped.
  Validated against a real NSDL e-CAS. Remaining hardening (other issuers/depositories, and
  values that wrap to the next line) is cleaner via pdfplumber **word/table coordinates** than
  text regex — a known future path. Note: holding numeric tokens use `_HOLDING_NUM_RE` (3–4
  decimals for NAV/units), not the 2-decimal `_AMOUNT_RE` used for money totals. A *summary*
  CAS has no per-holding rows to explode. Shares decrypt/text/float helpers with the CAMS
  parser via `app/parser/_common.py`.

- **`app/parser/cams_cas.py`** — sibling parser for a **CAMS / KFintech mutual-fund CAS**
  (MF-only, all AMCs). `parse_cams()` anchors on the ISIN scheme line and reads "Closing Unit
  Balance / NAV on <date> / Market Value on <date>". Classifies with `classify(section=UNKNOWN)`
  **on purpose** — so its keyword rules run first and gold/silver *funds* route to `GOLD`/`SILVER`
  instead of the MF default. Shares decrypt/text/float helpers with the NSDL parser via
  `app/parser/_common.py` (`nsdl_cas` keeps `_decrypt`/`_to_float` aliases for its tests). Same
  caveat as NSDL: built to the documented layout, snippet-tested, not yet validated on a real PDF.

- **`app/networth.py` + the Networth pages** (`/networth`, `/networth/{path}`) — a declarative
  Assets/Liabilities tree; leaves are blank scaffolds except the **data-backed** ones in
  `LEAF_ASSET_CLASSES` (Mutual Funds, Gold & Silver), which render holdings from two sources: an
  uploaded CAMS import and/or the latest NSDL snapshot's classified rows. `POST
  /networth/import/cams` parses a CAMS PDF and stores it via `replace_networth_import`. **Invariant:
  a CAMS import is NOT a `Snapshot`** — it lives in its own `networth_holdings` table so it can
  never land on the dashboard net-worth timeline (a snapshot means *total* net worth; CAMS is
  MF-only). MF precedence: CAMS supersedes NSDL (avoids double-count); Gold & Silver unions both,
  deduped by ISIN.

- **`app/storage.py`** — SQLite persistence. `upsert_snapshot()` keys on `statement_date`, so
  **re-uploading a statement for the same date replaces the existing snapshot** rather than
  duplicating; it returns the row id so `replace_holdings()` can attach the detailed rows. The
  `holdings` table cascades on snapshot delete and preserves CAS order via a `position` column;
  `list_accounts()` reassembles the Account→Holding tree. `list_snapshots()` returns oldest-first
  (chart-ready); the dashboard reverses for the newest-first table. Note: the `holdings` table is
  created *after* the legacy migration in `init_db`, so its FK isn't rewritten onto the dropped
  legacy snapshots table.

- **`app/classify.py`** — a layered, config-driven asset-class rule engine (section context >
  ISIN prefix > description keywords > manual override), **wired into `_find_accounts`** so each
  stored holding carries an `asset_class`. The
  deliberate trap it guards: corporate bonds/NCDs share the `INE` prefix with equity, so ISIN
  alone can't separate them — description keywords must.

- **`app/models.py`** — dataclasses shared across layers: `Holding`, `ParsedStatement` (parser
  output), `Snapshot` (stored row).

- **`app/wealth.py`** — the "Where do you stand?" feature (`GET /standing`). Static net-worth
  distribution data (adults per band for India/Indonesia/Singapore/USA/World, from
  `wealth_distribution.xlsx` — UBS/Knight Frank/Forbes) plus `rank_net_worth()`, which places a
  net worth within each geography by **piecewise power-law (Pareto) interpolation** between the
  known band edges (log-log linear between anchors; extrapolate the nearest segment's slope
  beyond the ends; clamp head-count to [top-band size, adult population]). This is the canonical,
  tested source. `client_dataset()` ships the raw constants to `static/standing.js`, which
  **re-implements the same algorithm** for live client-side ranking (verified identical to the
  Python) — so the interactive page never sends what a visitor types anywhere. Keep the two in
  sync if either changes; `test_wealth.py` pins the contract (band sums, monotonicity, anchor
  reproduction, the India-vs-USA contrast).

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
  **The one sanctioned exception** is `app/prices.py`: it fetches live equity quotes from Yahoo
  Finance and the *only* thing sent out is an exchange **ticker symbol** (e.g. "E2E") — never
  units, values, holding sizes, PAN, or identity. Keep it that way. Quotes are cached in-process
  ~15 min and every failure is swallowed (returns None) so a slow/blocked API never breaks a
  render. The ticker itself comes from the CAS (the `E2E.NSE` line under an equity row), captured
  onto `Holding.ticker` by the parser and stored in the `holdings.ticker` column; live price +
  gain-vs-statement is computed in `main._annotate_live_prices` and shown on the Networth Equity
  leaf. Only equities carry a ticker, so other leaves make no network call.
