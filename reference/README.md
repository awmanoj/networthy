# reference/

Source material for figures baked into the app. Public, aggregate reference data
only — **not** user data. (User statements and the parsed DB live under `data/`,
which is gitignored; nothing personal belongs here.)

## `wealth_distribution.xlsx`

The source for the net-worth distribution constants in **`app/wealth.py`**
(the "Where do you stand?" feature).

- **What it is:** adults per net-worth band for India, Indonesia, Singapore, the
  USA and the World. Two sheets — *Distribution* (Section 1: counts; Section 2:
  shares) and *Methodology & Sources*.
- **Provenance:** modeled from UBS Global Wealth Report 2025/26, Knight Frank
  Wealth Report, and Forbes Billionaires, with a Pareto tail fitted between known
  anchors. Adults age 20+. Modeled estimates, not a census — treat the top bands
  as order-of-magnitude.
- **Units / FX:** bands defined in INR crore; USD equivalents at **₹96.5 / $1**
  (RBI reference rate, ~Jul 2026).

### How it relates to the code

The numbers were transcribed **once, by hand** into literal constants in
`app/wealth.py` (`BAND_COUNTS`, `BAND_EDGES_CR`, `GEO_META` adult populations,
band labels). The app never reads this file at runtime — it's kept here purely for
provenance and future updates. Only Section 1 (counts) was copied; Section 2
(shares) is recomputed in code so there's a single source of truth.

If you refresh these figures (e.g. a newer UBS report), edit the literals in
`app/wealth.py` to match this workbook. The guard
`tests/test_wealth.py::test_band_counts_reconcile_to_population` asserts every
geography's bands still sum to its adult population, which catches transcription
slips.

> Note: `.xlsx` is a binary, so git diffs show "file changed," not which cells —
> lean on the reconciliation test when updating.
