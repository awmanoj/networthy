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
upload PDF(s)  →  parse_cas()  →  Snapshot + Accounts/Holdings  →  SQLite  →  NSDL CAS page (chart + holdings)
```

- **`app/main.py`** — FastAPI routes. The nav is just **Dashboard** + **NSDL CAS**. **Home `/` is
  the Dashboard** (`main.home`). **`GET /nsdl-cas`** (`main.nsdl_cas`, template `index.html`) holds
  the net-worth-over-time **chart + snapshots table**, an **Upload CAS** button, *and* the latest
  statement's **detailed per-account holdings** (the former Portfolio page, folded in here — colour-
  coded by asset class); `GET /portfolio` 307-redirects to it. `POST /upload` takes N files + one
  shared password (the PAN — all of a person's CAS PDFs use the same one) and parses each
  independently: one bad file doesn't sink the batch (200 if any saved, 400 only if all fail). It
  stores both the `Snapshot` and its detailed per-holding rows (`replace_holdings`). Delete routes:
  per-row `POST /snapshots/{id}/delete` and `POST /snapshots/delete-all` (both redirect to
  `/nsdl-cas`).

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

- **`app/networth.py` + the Networth pages** — a declarative Assets/Liabilities tree. The **home
  page is a live net-worth Dashboard** (`GET /` → `main.home` + `main._dashboard`, template
  `networth.html`): a hero total (Assets − Liabilities, summed live from the tree via
  `_networth_values`), an allocation strip + legend over the funded top-level categories, category
  tiles, a "Where do you stand?" CTA, and a "where it sits" list. Empty scaffold categories are
  omitted from the Dashboard. The full nested tree is reached by drilling in: detail pages stay at
  `/networth/{path}`, and `/networth` 307-redirects to `/`. Leaves are blank scaffolds except the
  **data-backed** ones in
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

- **`app/wealth.py`** — the "Where do you stand?" feature, a **full page at `GET /standing`**
  reached from the Dashboard CTA tile; pre-fills with the user's live sum-the-tree net worth. Static net-worth
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

- **Design system** (`static/style.css`): "Ink Navy & Copper" — one **token** set on `:root`
  (`--bg/--surface/--ink/--brand/--copper/--gain/--loss/--c-*` asset-class hues) drives everything.
  Both **light and dark** themes are defined by redefining those tokens under
  `@media (prefers-color-scheme: dark)` and `:root[data-theme="dark|light"]`; the nav toggle stamps
  `data-theme` and persists it in `localStorage` (`nw-theme`), applied pre-paint in `base.html`.
  Style components through tokens, never hardcode theme colors. Legacy names (`--text/--accent/--up/
  --down`) are aliased to the new tokens. Figures use `var(--serif)` (a system serif) for gravitas;
  UI chrome stays system-sans. Buttons go through `--btn-bg/--btn-fg` for dark-mode legibility.
- Amounts are INR, formatted with Indian digit grouping (`_to_float` strips lakh/crore commas
  like `12,34,567.89`).
- The privacy invariant is load-bearing: never add code paths that write statement contents or
  parsed financial data anywhere outside `data/`, and keep `data/` / `*.pdf` / `*.db` gitignored.
  **The one sanctioned exception** is `app/prices.py`, and it is kept deliberately narrow:
  - *Equities* — live quotes from Yahoo Finance; the *only* thing sent out is an exchange
    **ticker symbol** (e.g. "E2E") — never units, values, holding sizes, PAN, or identity. The
    ticker comes from the CAS (`E2E.NSE` line under an equity row), captured onto `Holding.ticker`
    by the parser and stored in `holdings.ticker`. Quotes cached ~15 min.
  - *Mutual funds (and gold funds)* — live NAV from AMFI's public **bulk** feed
    (`portal.amfiindia.com/spages/NAVAll.txt`). We download the whole file and look ISINs up
    **locally**, so nothing about the user's holdings is sent at all. The parsed ISIN→NAV map is
    cached ~6 h (NAV publishes once daily).
  - *Foreign (US) equity* — hand-entered ticker + shares, priced live in USD from Yahoo (same
    helper) and converted to INR at the live `INR=X` rate (`prices.usd_inr()`). Only the ticker
    symbol egresses, same as Indian equity. Stored in its own `foreign_holdings` table; priced in
    `main._price_foreign`.
  Every failure is swallowed (returns None/empty) so a slow/blocked/changed endpoint never breaks a
  render — the view falls back to statement values. Enrichment (live price, live value,
  gain-vs-statement, up/down/flat signal) is computed in `main._annotate_live_prices` — equity
  ticker → Yahoo, any other ISIN holding → AMFI — and shown on the Networth leaf tables with a
  source-accurate provenance note. Keep this the only module that egresses. Note: the Networth
  **overview and node pages** also trigger this (via `networth.rollup` + `_leaf_value`) to show a
  **live-consistent value against each head** — so the head number matches the leaf's live total
  rather than the statement value. That means `/networth` makes the same (cached) calls; the
  rollup sums each leaf's value up its parents.
- **Manual Networth entries**: leaves in `networth.MANUAL_LEAVES` (PPF, EPF, SSA, NSC, FDs,
  Other Fixed Income, Others — plus Corporate Bonds / Govt Bonds / NPS, which *also* auto-fill
  from the NSDL CAS) accept hand-entered rows via `manual_holdings` (per-leaf: scheme,
  investment_amount, and optional maturity_amount / investment_date / maturity_date / rate).
  `investment_amount` is the current value that rolls into net worth; the rest are informational.
  Tenure is *derived* from the two dates at display time (`main._enrich_manual`), not stored — the
  legacy `years` column is kept only as a fallback for rows entered before dates existed. `_leaf_value` /
  `_leaf_holdings` combine CAS-live holdings **and** manual rows, so a leaf like Corporate Bonds
  shows both. Add via `POST /networth/manual/add`, remove via `POST /networth/manual/{id}/delete`
  (both carry a `redirect` = the leaf's slug-path, validated through `networth.resolve`).
- **Other hand-entered leaves** (own shapes, handled specially in `main`, not via `manual_holdings`):
  *Foreign / US Equity* (`FOREIGN_LEAF`) — ticker + shares in `foreign_holdings`, priced live
  (see prices.py). *Bank Accounts & Cash* — `networth.BANK_CASH_LEAVES` = {`bank-accounts`, `cash`}
  under the `bank-cash` category; rows in the `bank_cash` table (bank_name, account_type, label,
  balance — **no account number is stored, by design**). *Foreign Exchange* (`FOREX_LEAF` =
  `foreign-exchange`) — money in a foreign currency held in an account or as cash, in the
  `forex_holdings` table (currency, amount, kind, label), valued live via `prices.fx_to_inr(cur)`
  (Yahoo `<CUR>INR=X`) in `main._price_forex`; routes `POST /networth/forex/add` + `/forex/{id}/delete`.
  All of these roll up the tree via `_leaf_value` alongside CAS/manual leaves; only the ticker /
  currency pair egresses.
