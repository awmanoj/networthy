"""Recurring-expense model: categories, frequencies, and annualisation.

Expenses are a separate lens from net worth — a recurring-spend planner, not a
transaction ledger and not part of the Assets/Liabilities tree. Each entry is an
amount at a cadence, optionally multiplied by a count (the lightweight
family-scaling lever: "school fees × 2 kids", "health cover × 4 members"). We
normalise everything to an annual (and monthly) burn rate, which is what connects
to net worth via runway and a FIRE target.

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

# The FIRE rule of thumb: 25× annual expenses ≈ a 4% safe withdrawal rate.
FIRE_MULTIPLE = 25


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
