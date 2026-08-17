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

- **Public marketing surface**: `GET /` serves a **landing page** (`landing.html` on `marketing_base.html`
  — its own header with Log in / Get started and a footer of About/Privacy/Terms) when logged out, and
  the **Dashboard** when logged in (`main.home` branches on `request.state.user`). `/about`, `/privacy`,
  `/terms` are content pages. All four, plus `/`, are in `auth._PUBLIC_PATHS`; every other route stays
  gated (anonymous → `/login`). Sign-up == login (open signup via email OTP), so the primary landing CTA
  points at `/login`; a secondary **"Explore the live demo"** CTA points at `/demo`. The landing's
  "screenshots" are on-brand HTML/CSS mockups (`.shot` frames) built from the design tokens — swap for
  real captures if desired.
- **Public demo** (`app/demo.py`, `GET /demo` — in `_PUBLIC_PATHS`): a one-click, no-signup entry into a
  shared **`demo@networthyhq.com`** account. The route **resets** the account to a fixed fixture on every
  entry (`demo.reset` → `storage.clear_user_tables` then re-seed, so it's always clean no matter how the
  last visitor poked at it), opens a session via `auth.start_session`, and redirects to the Dashboard.
  The fixture covers popular categories (real estate, MFs via a CAMS import, fixed income, bank/cash,
  gold, US equity + crypto — which price *live* on the server — an angel investment, a home loan, expenses
  and goals, plus NSDL snapshots for the trend chart). `base.html` shows a copper **demo banner** whenever
  `user.email == demo.DEMO_EMAIL`. Note: the session cookie is `Secure`, so the demo login only "sticks"
  over HTTPS (tests set `auth.cookie_secure=lambda: False`). **Both** the app base (`base.html`) and
  `marketing_base.html` include the shared `_footer.html` (About/Privacy/Terms) — edit the footer in one
  place. The **display brand is "Networthy HQ"** across templates and emails (the project/module name
  stays `networthy`).
- **`app/main.py`** — FastAPI routes. The in-app nav is just **Dashboard** + **NSDL CAS**. **Home `/` is
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

- **`app/networth.py` + the Networth pages** — a declarative Assets/Liabilities tree. Two views of
  it: the **Dashboard** (`GET /` → `main.home` + `main._dashboard`, template `networth.html`) is the
  at-a-glance home — a hero total (Assets − Liabilities, summed live via `_networth_values`), an
  allocation strip, a **net-worth-over-time trend chart**, category tiles, a "Where do you stand?"
  CTA, and a "where it sits" list (empty categories omitted). The **trend** is the forward series from
  `nw_history`: `main.home` **bootstraps** it by recording today's point on each view
  (`storage.ensure_nw_point`, insert-if-absent per IST day — so a dashboard visit builds history even
  before the digest cron runs, and never clobbers the digest's richer breakdown row), then ships the
  full series to `networth.html`, whose inline script drives `chart.js` with **range toggles**
  (1M/3M/6M/1Y/All) and a Δ-over-range readout. No investment-date reconstruction — it accumulates
  forward from first use (shows a "trend builds" note until there are ≥2 points). The **Net worth hub** (`GET /networth` → `main.networth_overview`, template
  `networth_overview.html`) is the structured entry point — the net-worth summary plus the **full
  tree** (every section/subsection/leaf, incl. empty scaffolds) as navigable links with rolled
  values and "N inside" chips. Leaf/detail pages are at `/networth/{path}`; breadcrumbs root at the
  Net worth hub. Nav order: Dashboard · Net worth · Expenses · Goals · Plan · NSDL CAS. Leaves are blank scaffolds
  except the **data-backed** ones in
  `LEAF_ASSET_CLASSES` (Mutual Funds, Gold & Silver), which render holdings from two sources: an
  uploaded CAMS import and/or the latest NSDL snapshot's classified rows. `POST
  /networth/import/cams` parses a CAMS PDF and stores it via `replace_networth_import`. **Invariant:
  a CAMS import is NOT a `Snapshot`** — it lives in its own `networth_holdings` table so it can
  never land on the dashboard net-worth timeline (a snapshot means *total* net worth; CAMS is
  MF-only). MF precedence: CAMS supersedes NSDL (avoids double-count); Gold & Silver unions both,
  deduped by ISIN. **CAMS import assist** (`cams_import.html`): the page (1) saves the user's PAN +
  CAS-registered email once (`user_settings` table, `get`/`save_user_settings`; PAN uppercased,
  stored **local-only** and used as the PDF password), (2) builds a **personalised auto-fill
  bookmarklet** (`_cams_bookmarklet`) that fills the live CAMS CAS form
  (`CAMS_CAS_PAGE`) in the user's own browser — reCAPTCHA Enterprise is bundled on that form but only
  loads on submit, so filling from the user's real browser (not a headless server) is the point — and
  (3) uploads the emailed PDF, where `password` **falls back to the saved PAN** when blank. Manual
  step-by-step is kept for anyone the bookmarklet doesn't suit. CAMS mails the PDF to the *registered*
  email asynchronously; the **Summary** statement (balances + valuation, as-on today) is enough for
  `parse_cams`. (We deliberately did **not** build Gmail auto-ingest — the receiving side stays a
  manual upload by choice.)

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

- **`app/expenses.py` + the Expenses tab** (`GET /expenses`, top-level nav between Net worth and
  Goals) — a recurring-expense **planner**, deliberately *separate from net worth* (its own
  `expenses` table; not in the Assets/Liabilities tree). Each entry is amount × **count** (the
  per-person family-scaling lever) at a **frequency** (`FREQUENCIES`: monthly/quarterly/half-yearly/
  annual), normalised to a monthly & annual burn via `annual_amount()`. The page shows the burn,
  a category breakdown (`CATEGORIES`, fixed list with per-category colours), and the **net-worth
  connection**: runway (net worth ÷ annual burn) and a **FIRE target** with progress. Loan EMIs are
  intentionally **not** modelled here — they live under Liabilities, and double-counting would make
  burn-rate and net-worth views disagree. Routes: `POST /expenses/add` + `/expenses/{id}/delete`.
  **Safe withdrawal rate**: the FIRE target is driven by a **per-user rate**, not a constant —
  `DEFAULT_SWR_PCT` = **3.0** (33×), deliberately *not* the US 4%/25× rule, which is a Trinity-study
  result (US 1926–95, 30-year horizon, ~3% inflation, Social Security underneath) and optimistic
  against India's ~6% general / higher healthcare inflation and absent state pension. `normalise_swr`
  (default-if-unset, clamped to `SWR_MIN_PCT`..`SWR_MAX_PCT`), `swr_multiple` (100/rate) and
  `fire_target` are the API; `SWR_PRESETS` (2.5 / 3.0 / 3.5 / 4.0, each with a one-line rationale)
  drives the **ladder** on the Expenses page (`main._swr_ladder` — the target and % progress at every
  preset, plus the user's own rate if it isn't one, so the assumption reads as a *range*). Stored in
  `user_settings.swr_pct` via `get`/`save_swr_pct` — kept as **separate accessors** from
  `get`/`save_user_settings` (the CAMS PAN/email pair) so neither upsert clobbers the other's columns.
  Set via `POST /expenses/swr`. Goals' read-only Retirement card reads the same setting, so there is
  one source of truth for the assumption.

- **`app/goals.py` + the Goals tab** (`GET /goals` → `goals_page`, template `goals.html`; nav order is
  Dashboard · Net worth · Expenses · **Goals** · NSDL CAS) — a **target-by-date planner**, another
  lens separate from the net-worth tree (own `goals` table). Each goal is a target amount + date +
  expected return + **saved-so-far** (hand-entered — we deliberately don't tag holdings to goals, which
  avoids double-counting one rupee across goals). `goals.plan()` is the core: it compounds saved-so-far
  to the target date and returns the **required monthly SIP** (future-value-of-annuity) **and a
  `required_lumpsum`** — the one-time amount today that, compounding at the same rate, closes the same
  gap (`gap / (1+r)^n`; the "set it aside now" alternative to the SIP) — plus a status —
  `funded` (projection alone reaches target), `active` (show the SIP + lump sum), `overdue` (date passed,
  not funded), `undated` (no date → progress only). The summary also totals both across dated goals. Return is stored per-goal as a percent
  (`DEFAULT_RETURN_PCT` = 10 if blank). `CATEGORIES` gives each goal type a colour + icon. **Retirement
  is not stored** — it's mirrored **read-only** at the top of the list from the Expenses FIRE target
  (annual burn ÷ the user's withdrawal rate, default 3% → 33×; progress = live net worth), so both the
  burn and the SWR assumption have a single source of truth; the card links back to Expenses to change it. Add/edit/delete reuse the shared edit flow (`update_row` + `.edit-glow`):
  `POST /goals/add` (add-or-update via optional `id`) + `/goals/{id}/delete`. `test_goals.py` pins the
  SIP math (annuity formula, funded/undated/overdue branches, month counting) and the route round-trips.

- **`app/projection.py` + the Plan tab** (`GET /plan` → `main.plan_page`, template `plan.html`; nav
  order is Dashboard · Net worth · Expenses · Goals · **Plan** · NSDL CAS) — the lifetime cash-flow
  projection, the one forward-looking view in the app. **It adds no new data model**: the starting
  corpus is the live net worth, the recurring draw is the Expenses annual burn, and one-off outflows
  are **dated `goals` rows** (`outflows_from_goals` — undated, past, zero-amount and beyond-horizon
  goals are skipped). Only four inputs are new, stored in `user_settings` (`plan_birth_year`,
  `plan_retire_age`, `plan_annual_savings`, `plan_return_pct`, `plan_inflation_pct`) via
  `get`/`save_plan_settings` — again **separate accessors** from the CAMS pair and the SWR one, so no
  upsert clobbers another's columns. Age is stored as a **birth year** so a saved plan doesn't
  under-age the user every January. Three modelling decisions carry the feature and are pinned by
  `test_projection.py`: (1) **savings before retirement, expenses after** — `annual_savings` is
  already net of living costs, so charging the burn during accumulation would double-count it;
  (2) **goals are pure nominal outflows** at the amount entered — a goal that buys an asset (a house)
  is *not* modelled as acquiring one, and the UI says to enter the deposit as the goal and the EMI
  under Expenses; (3) **the UI shows today's money** (`YearPoint.real_closing`, `*_real` summary
  keys) — a nominal balance 50 years out is mostly inflation and reads as a bug ("₹581 crore at 95"),
  while the per-year Added/Drawn/Goals columns stay in that year's rupees. `corpus_requirement()` answers the
  question the depletion age raises but doesn't settle — "runs out at 75" vs "you're ₹94 lakh short
  **today**" — by **bisecting** on the starting corpus (the year loop clamps at zero, mixes inflating
  flows with fixed-date outflows and switches regime at retirement, so it doesn't invert cleanly; it
  *is* monotonic in corpus, which is all bisection needs). It returns the same shape either way: a
  negative `gap` is the cushion above the minimum. The test that matters is the round-trip — topping
  the corpus up by exactly `gap` makes the plan reach 95, and a little less doesn't. `project_band()`
  runs the model at return **±`BAND_DELTA_PCT`** and the page reports all three outcomes plus the
  requirement at each, because a single line to 95 reads as a forecast; a plan can last past 95 at 10% and run dry at 90 at 8%, and that
  disagreement is the output. **No tax is modelled at all** — disclosed on the page via
  `fine_print`. Chart is `static/plan-chart.js` (deliberately separate from `chart.js`: age-indexed,
  three series, event markers).

- **`app/wealth.py`** — the "Where do you stand?" feature, a **public full page at
  `GET /how-rich-am-i`** (`main.how_rich_am_i`, template `standing.html`) reached from the Dashboard
  CTA tile, the landing page and the footer; pre-fills with the user's live sum-the-tree net worth
  when signed in. Static net-worth
  distribution data (adults per band for India/USA/Singapore/Australia/Canada/Indonesia/Japan/World —
  the `GEO_ORDER` display order — from `wealth_distribution.xlsx` plus modeled UBS/Knight Frank/Forbes
  estimates for the added markets) plus `rank_net_worth()`, which places a
  net worth within each geography by **piecewise power-law (Pareto) interpolation** between the
  known band edges (log-log linear between anchors; extrapolate the nearest segment's slope
  beyond the ends; clamp head-count to [top-band size, adult population]). This is the canonical,
  tested source. `client_dataset()` ships the raw constants to `static/standing.js`, which
  **re-implements the same algorithm** for live client-side ranking (verified identical to the
  Python) — so the interactive page never sends what a visitor types anywhere. Each `Placement`
  also carries a **within-band split** (`band_total`/`band_above`/`band_below`): a band is a wide
  range whose population clusters near its floor, so the raw band total must **not** be read as
  "peers at your level" — the pyramid's "you're here" band shows ≈above-you / ≈below-you instead.
  Keep the two in sync if either changes; `test_wealth.py` pins the contract (band sums,
  monotonicity, anchor reproduction, the band split partition, the India-vs-USA contrast).
  `wealth_for_top_pct(pct, geo)` is the **exact inverse** ("the top 1% starts at ₹1.92 cr") —
  each power-law segment inverted in closed form, not bisected, so it cannot drift from the
  forward ranking (`test_wealth.py` round-trips it through `place_one` across every geography and
  every segment). It is **server-side only** — `standing.js` mirrors only the forward ranking,
  because the inverse feeds a static table, so there's no JS twin to keep in sync.

- **Disclaimers** — three layers, deliberately. (1) `_footer.html` carries a one-line global
  "indicative estimates, not financial advice" that appears on **every** page via both bases.
  (2) `_notes.html` exports a `fine_print(text)` macro — one place to word the caveat + the Terms
  link — used on every page showing a *modelled or projected* number (Expenses' withdrawal-rate
  card, Goals' SIP/lump-sum maths, the ranking page). (3) The public ranking page states its
  **sourcing in context** (UBS / Knight Frank / Forbes, the ₹96.5-to-$1 rate, the Pareto
  interpolation, and what the model can't see) rather than relying on `/terms`, because a visitor
  arriving from search will never open Terms — and a money page with no visible provenance is
  exactly what search quality raters penalise. Terms keeps the formal version. Live-price
  provenance is separate and already per-leaf (`main._LIVE_NOTES`). `test_seo.py` pins all three
  layers.

- **SEO / the indexable surface** — the ranking page is the one piece of top-of-funnel content, so
  it is **public** (in `auth._PUBLIC_PATHS`) and deliberately crawlable. A crawler is an anonymous
  client with no JS, which drives three constraints: (1) the page must not slip back behind the
  session gate; (2) the explorer paints into empty divs from `standing.js`, so `main._standing_levels`
  / `_standing_bands` / `_standing_thresholds` **server-render** the reference tables — that's the
  only indexable text on the page. Order is deliberate: the **threshold** table ("top 1% starts at
  ₹1.92 crore", from `wealth.wealth_for_top_pct`) comes first because it answers the question the
  way people search it, with the figures also stated as **prose above the table** so a search engine
  can lift them as a snippet; the where-does-₹X-rank table is the same question inverted. Helpers:
  `_inr_short` (₹19,198,026 → "₹1.92 crore") and `_share_display` (millionth-of-a-percent bands read
  as "1 in N", not `2.05e-05%`); (3) `base.html` renders Log in /
  Get started in `.nav-right` when there's no `user` (not in `.nav-links`, which is collapsed behind
  the burger checkbox that only exists for signed-in users). The old **`/standing` URL 301s** to the
  new one — permanent, so the move carries its signals (contrast `/portfolio` → `/nsdl-cas`, a 307,
  which is fine because that route is gated and never indexed). Per-page meta is opt-in: a route
  passes `page_title` / `page_description` / `canonical_path` and `_head_meta.html` (included by
  **both** bases, and the single owner of description + canonical + OG — don't re-add a description
  to `marketing_base.html`) falls back to the site-wide defaults. `GET /robots.txt` and
  `GET /sitemap.xml` are generated in `main` from `_SITEMAP_PATHS`; **every path listed there must be
  in `_PUBLIC_PATHS`** (`test_seo.py` asserts exactly that, plus the 301, anonymous reachability, the
  server-rendered numbers, and that the app routes are still gated).

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
- **Business analytics** (`app/analytics.py` + `GET /admin`, template `admin.html`): owner-only
  adoption metrics — signups, sign-ins (DAU/WAU/MAU from the durable `login_events` table, since
  `sessions` rows vanish on logout), returning users, feature adoption, an activation funnel, and a
  users/email listing. **First-party and metadata-only by design**: it reads the app's own tables,
  nothing egresses, and it deliberately carries **no financial values** (counts, not amounts) — so this
  surface leaks no holdings even if breached. The **demo account is excluded** from every number. Gate:
  `user.email == auth.owner_email()` (env `OWNER_EMAIL`, default `awasthi.manoj@gmail.com`); non-owners
  get a 404 (existence hidden), anonymous → `/login`. Disclosed on the Privacy page. Served at
  **analytics.networthyhq.com** via a Caddy vhost that redirects to `/admin` on the apex (so the owner's
  session cookie — set host-only on the apex — is valid; a subdomain reverse-proxy would need a
  `.networthyhq.com` cookie domain):
  ```
  analytics.networthyhq.com { redir https://networthyhq.com/admin }
  ```
  Distinct from the existing **stats.networthyhq.com** (GoAccess web-traffic analytics from Caddy logs):
  that's anonymous page-view traffic; this is per-account product adoption. Both stay first-party.
- **Email digests** (`app/digest.py`): `python -m app.digest daily|weekly` recomputes every user's net
  worth live, records a **daily snapshot** in `nw_history`, and emails a change summary via `mailer`
  (no-ops to a log without `RESEND_API_KEY`). Daily = day-over-day net-worth delta; weekly = that plus a
  breakdown of the four live-priced categories (MF, Equity, Foreign Equity, Crypto). Subjects carry the
  **change, not the total** (keeps the figure off lock-screen previews). Cron (server is UTC; 6 PM IST =
  12:30 UTC — daily Mon–Sat, weekly Sunday so you never get two in one day):
  ```
  30 12 * * 1-6 docker exec networthy python -m app.digest daily  >> /var/log/networthy-digest.log 2>&1
  30 12 * * 0   docker exec networthy python -m app.digest weekly >> /var/log/networthy-digest.log 2>&1
  ```
  `nw_history` (one row per IST day per user) also gives the deferred net-worth-over-time trend.
- `backup.sh` snapshots the DB via SQLite's **online backup** (run inside the app container's
  Python, so it's consistent under concurrent writes — not a raw file copy), gzips it into
  `$BACKUP_DIR`, and prunes beyond `$KEEP` (default 42 ≈ 7 days at every 4h). Cron it every 4h:
  `0 */4 * * * /path/to/backup.sh >> /var/log/networthy-backup.log 2>&1`. Backups land on the
  same host — copy them off-box (rclone/S3) to survive server loss.

## Conventions

- **Design system** (`static/style.css`): "Ink Navy & Copper" — one **token** set on `:root`
  (`--bg/--surface/--ink/--brand/--copper/--gain/--loss/--c-*` asset-class hues) drives everything.
  Both **light and dark** themes are defined by redefining those tokens under
  `@media (prefers-color-scheme: dark)` and `:root[data-theme="dark|light"]`; the nav toggle stamps
  `data-theme` and persists it in `localStorage` (`nw-theme`), applied pre-paint in `base.html`.
  Style components through tokens, never hardcode theme colors. Legacy names (`--text/--accent/--up/
  --down`) are aliased to the new tokens. Figures use `var(--serif)` (a system serif) for gravitas;
  UI chrome stays system-sans. Buttons go through `--btn-bg/--btn-fg` for dark-mode legibility.
  **Responsive**: mobile-usable, not app-shell — the app nav collapses to a **CSS-only hamburger**
  (a hidden `#nav-toggle` checkbox in `base.html`; `:checked ~ .nav-links` opens the dropdown, no JS),
  wide `.holdings`/`.snapshots` tables scroll inside their card (`display:block; overflow-x:auto` under
  640px) rather than widening the page, and multi-column grids (allocation legend, dashboard tiles)
  stack. **Gotcha**: same-specificity media overrides must come *after* the base grid rule, so the
  mobile block lives at the **end of `style.css`** (source order wins). Verify changes don't reintroduce
  horizontal scroll at 390px (`document.documentElement.scrollWidth <= clientWidth`).
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
- **Editing manual entries** (every Networth manual type *and* Expenses): there is no separate
  edit route — each `/…/add` route is add-*or*-update. It takes an optional `id` form field; when
  present it calls the one generic `storage.update_row(table, row_id, user_id, **fields)` (owner-
  scoped `UPDATE`; table + column names are code-controlled so interpolating them is safe, values
  bound), otherwise it inserts as before. The UI is a pre-filled-form pattern, no JS: a row's
  **edit** link reloads the page with `?edit={id}`; the leaf page (or Expenses category section)
  reads it as `edit_id` (via `_opt_int`) and the matching add-form pre-fills from that row —
  heading flips to "Edit…", the button becomes "Save", a **Cancel** link (`href="?"`) clears the
  query, and the row highlights (`.row-editing`). Edit writes exactly the same column set the add
  path does (each route builds one `f` dict used for both), so anything not in that route's form —
  e.g. the `position` ordering column, or `notes` on Expenses, which has no notes input — is never
  disturbed. `test_edit.py` covers the `update_row` contract (columns changed, others untouched,
  owner-scoped, no-field no-op) plus an end-to-end prefill→save round-trip.
- **Tiny direct-equity filter**: `_leaf_rows` drops `direct_equity` holdings whose statement value
  is `< MIN_EQUITY_VALUE` (₹10,000) — tracking-only positions (a stray share or two) that
  shouldn't count toward net worth or clutter the Equity leaf. Applied before valuation, so both
  the Equity leaf total and the net-worth rollup exclude them. The NSDL CAS page shows the raw
  statement unfiltered.
- **Other hand-entered leaves** (own shapes, handled specially in `main`, not via `manual_holdings`):
  *Foreign / US Equity* (`FOREIGN_LEAF`) — ticker + shares in `foreign_holdings`, priced live
  (see prices.py). *Bank Accounts & Cash* — `networth.BANK_CASH_LEAVES` = {`bank-accounts`, `cash`}
  under the `bank-cash` category; rows in the `bank_cash` table (bank_name, account_type, label,
  balance — **no account number is stored, by design**). *Foreign Exchange* (`FOREX_LEAF` =
  `foreign-exchange`) — money in a foreign currency held in an account or as cash, in the
  `forex_holdings` table (currency, amount, kind, label), valued live via `prices.fx_to_inr(cur)`
  (Yahoo `<CUR>INR=X`) in `main._price_forex`; routes `POST /networth/forex/add` + `/forex/{id}/delete`.
  *Crypto* (`CRYPTO_LEAF` = `crypto`, a leaf under Financial Assets) — coin + quantity in the
  `crypto_holdings` table (symbol, quantity, invested_inr, label), priced live via
  `prices.crypto_inr(sym)` = Yahoo `<SYM>-USD` × USD→INR in `main._price_crypto`; optional
  `invested_inr` drives gain%; routes `POST /networth/crypto/add` + `/crypto/{id}/delete`.
  (Crypto is *also* a hand-valued Type under Alternate Investments, for illiquid/locked positions.)
  *Alternate Investments* (`ALT_LEAF` = `alternate-investments`) — illiquid hand-valued bets
  (startups/angel, ESOPs, unlisted, PE/VC, crypto) in the `alt_investments` table (name, category,
  cost, current_value, invested_date); `current_value` rolls into net worth, `cost` drives a
  gain% (`main._enrich_alt`); routes `POST /networth/alt/add` + `/alt/{id}/delete`. No live price.
  *Real Estate* — `networth.REALTY_LEAVES` (the five sub-leaves under `real-estate`); hand-entered
  properties in the `property_holdings` table (leaf_slug, label, current_value, cost, purchase_date,
  notes, share_pct), reusing `_enrich_alt` for gain/date; `current_value` is **gross** (a loan
  against it lives under Liabilities and net worth already nets it). `share_pct` (NULL = 100%)
  attributes joint property — net worth counts `current_value × share%` (`main._property_share`);
  the leaf shows Share/Your-value columns only when something is jointly held. Routes
  `POST /networth/property/add` + `/property/{id}/delete`. *Physical Gold & Jewellery* (`GOLD_LEAF` = `physical-gold`) —
  `gold_items` table; each item is **either** weight+karat (valued live at
  `prices.gold_inr_per_gram()`, derived from `GC=F`×`INR=X`, × karat purity in `main._price_gold`)
  **or** a flat hand-entered value; routes `POST /networth/gold/add` + `/gold/{id}/delete`.
  *Private Business* (`BUSINESS_LEAF` = `private-business`) — `business_holdings` (name,
  ownership_pct, cost, current_value, invested_date, notes), gain via `_enrich_alt`; routes
  `POST /networth/business/add` + `/business/{id}/delete`. *Liabilities* — `networth.LIABILITY_LEAVES`
  (all 9 loan/dues leaves); hand-entered in the `liabilities` table (lender, **outstanding**,
  principal, rate, emi, end_date, notes); **`outstanding` is what net worth subtracts** (not the
  principal borrowed), enriched by `main._enrich_liability` (% paid off from principal, remaining
  tenure from end_date); routes `POST /networth/liability/add` + `/liability/{id}/delete`. Assets
  roll up to `values["assets"]`, liabilities to `values["liabilities"]`, and the hero shows
  Assets − Liabilities. All of these roll up the tree via `_leaf_value` alongside CAS/manual
  leaves; only tickers / currency pairs / gold+FX symbols egress.
