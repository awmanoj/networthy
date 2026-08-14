"""Recurring-expense model: categories, frequencies, and annualisation.

Expenses are a separate lens from net worth — a recurring-spend planner, not a
transaction ledger and not part of the Assets/Liabilities tree. Each entry is an
amount at a cadence, optionally multiplied by a count (the lightweight
family-scaling lever: "school fees × 2 kids", "health cover × 4 members"). We
normalise everything to an annual (and monthly) burn rate, which is what connects
to net worth via runway and a FIRE target (see the safe-withdrawal-rate block
below — the target is driven by a per-user rate, defaulting to an India-realistic
3%, not the US 4% rule).

Loan EMIs are deliberately *not* modelled here — they live under Liabilities, and
counting them in both places would make the two views disagree.
"""

from __future__ import annotations

# Frequency slug -> label + how many times a year it occurs (annualiser).
FREQUENCIES: dict[str, dict] = {
    "monthly": {"label": "Monthly", "per_year": 12},
    "quarterly": {"label": "Quarterly", "per_year": 4},
    "half-yearly": {"label": "Half-yearly", "per_year": 2},
    "annual": {"label": "Annual", "per_year": 1},
}

# Curated household categories, each with a distinct medium-tone colour that reads
# on both light and dark surfaces (used for the breakdown bar and dots).
CATEGORIES: list[dict] = [
    {"slug": "housing", "label": "Housing", "color": "#5b8fc9",
     "example": "e.g. Rent, maintenance, property tax"},
    {"slug": "utilities", "label": "Utilities", "color": "#7a8794",
     "example": "e.g. Electricity, internet, mobile, gas"},
    {"slug": "food", "label": "Food & Groceries", "color": "#3f9079",
     "example": "e.g. Groceries, milk, water can"},
    {"slug": "transport", "label": "Transport", "color": "#c79a52",
     "example": "e.g. Fuel, cab, metro, car service"},
    {"slug": "healthcare", "label": "Healthcare & Insurance", "color": "#4a9d9d",
     "example": "e.g. Health/term premium, medicines"},
    {"slug": "education", "label": "Education & Childcare", "color": "#6a7fb0",
     "example": "e.g. School fees, tuition, daycare"},
    {"slug": "domestic-help", "label": "Domestic Help", "color": "#a06a4e",
     "example": "e.g. Maid, cook, driver, nanny"},
    {"slug": "lifestyle", "label": "Lifestyle & Personal", "color": "#b06a8c",
     "example": "e.g. Clothing, salon, subscriptions"},
    {"slug": "dining", "label": "Dining & Entertainment", "color": "#cf7a4e",
     "example": "e.g. Eating out, OTT, movies"},
    {"slug": "travel", "label": "Travel & Vacations", "color": "#5aa6c6",
     "example": "e.g. Flights, hotels, annual trip"},
    {"slug": "gifting", "label": "Gifting & Donations", "color": "#8a6ea8",
     "example": "e.g. Gifts, festivals, charity"},
    {"slug": "other", "label": "Other", "color": "#8792a0",
     "example": "e.g. Anything else"},
]
CATEGORY_BY_SLUG: dict[str, dict] = {c["slug"]: c for c in CATEGORIES}

# --- Safe withdrawal rate ---------------------------------------------------
#
# A FIRE target is "the corpus from which I can withdraw my expenses, raised for
# inflation, without running out". The famous 25× / 4% figure is a *US* result —
# the Trinity study ran US stocks/bonds over 1926–1995, a 30-year horizon, with
# ~3% inflation and Social Security underneath. India differs on every one of
# those inputs: ~6% general inflation and double-digit healthcare inflation,
# a much shorter reliable-return history, and no state pension floor. So the
# default here is **3% (33×)**, not 4%, and the rate is a per-user assumption
# rather than a constant — the honest answer is a range, which is why the
# Expenses page also shows the target at each preset rate.
DEFAULT_SWR_PCT = 3.0

# Sanity bounds for a hand-entered rate (0% would be an infinite target).
SWR_MIN_PCT = 1.0
SWR_MAX_PCT = 10.0

SWR_PRESETS: list[dict] = [
    {"pct": 2.5, "label": "Conservative",
     "note": "Early retirement, a 40+ year horizon, or a corpus you can never top up."},
    {"pct": 3.0, "label": "India-realistic",
     "note": "The sane Indian default — ~6% inflation, healthcare rising faster, no state pension."},
    {"pct": 3.5, "label": "Moderate",
     "note": "Retiring later, or able to trim spending in bad years / earn a little on the side."},
    {"pct": 4.0, "label": "US · Trinity rule",
     "note": "The classic 4% rule, from US market history. Optimistic for an Indian portfolio."},
]


def normalise_swr(pct: float | None) -> float:
    """A usable withdrawal rate: the default when unset, clamped to sane bounds."""
    if pct is None:
        return DEFAULT_SWR_PCT
    try:
        val = float(pct)
    except (TypeError, ValueError):
        return DEFAULT_SWR_PCT
    if val <= 0:
        return DEFAULT_SWR_PCT
    return min(SWR_MAX_PCT, max(SWR_MIN_PCT, val))


def swr_multiple(pct: float | None) -> float:
    """The corpus multiple implied by a withdrawal rate: 4% → 25×, 3% → 33.3×."""
    return 100.0 / normalise_swr(pct)


def fire_target(annual_expense: float, pct: float | None) -> float:
    """The corpus that sustains this annual burn at the given withdrawal rate."""
    return (annual_expense or 0.0) * swr_multiple(pct)


def annual_amount(amount: float | None, count: int | None, frequency: str) -> float:
    """Normalise one expense to a yearly figure: amount × count × times-per-year."""
    per_year = FREQUENCIES.get(frequency, {}).get("per_year", 0)
    return (amount or 0.0) * (count or 1) * per_year


def category_label(slug: str) -> str:
    cat = CATEGORY_BY_SLUG.get(slug)
    return cat["label"] if cat else (slug or "Other")


def category_color(slug: str) -> str:
    cat = CATEGORY_BY_SLUG.get(slug)
    return cat["color"] if cat else "#8792a0"


def frequency_label(slug: str) -> str:
    freq = FREQUENCIES.get(slug)
    return freq["label"] if freq else slug
