"""Financial goals: a target amount by a target date, and the monthly investment
needed to get there.

A goal is a separate planning lens (like Expenses), not part of the Assets/
Liabilities tree. Each goal tracks a target, a date, an expected return, and how
much you've saved toward it *so far* (entered by hand — we deliberately don't tag
holdings to goals in v1, which keeps it simple and avoids double-counting one rupee
across several goals). From those we derive the headline number: the **required
monthly SIP** to hit the target on time, plus an at-a-glance status.

The retirement goal isn't stored here — it's mirrored read-only from the Expenses
FIRE target (25× annual burn) so there's a single source of truth for the burn.
"""

from __future__ import annotations

from datetime import date

# Goal types, each with a distinct medium-tone colour that reads on light + dark
# (used for the accent dot / progress bar), and an icon + example for the form.
CATEGORIES: list[dict] = [
    {"slug": "retirement", "label": "Retirement", "color": "#b06a3c", "icon": "🏖️",
     "example": "e.g. Retirement corpus"},
    {"slug": "home", "label": "Home / Property", "color": "#5b8fc9", "icon": "🏠",
     "example": "e.g. House down-payment"},
    {"slug": "education", "label": "Education", "color": "#3f9079", "icon": "🎓",
     "example": "e.g. Kids' college fund"},
    {"slug": "vehicle", "label": "Vehicle", "color": "#c79a52", "icon": "🚗",
     "example": "e.g. New car"},
    {"slug": "travel", "label": "Travel", "color": "#5aa6c6", "icon": "✈️",
     "example": "e.g. Europe trip"},
    {"slug": "emergency", "label": "Emergency Fund", "color": "#cf5a4e", "icon": "🛟",
     "example": "e.g. 6 months of expenses"},
    {"slug": "wealth", "label": "Wealth Target", "color": "#8a6ea8", "icon": "📈",
     "example": "e.g. First ₹1 crore"},
    {"slug": "other", "label": "Other", "color": "#8792a0", "icon": "🎯",
     "example": "e.g. Anything else"},
]
CATEGORY_BY_SLUG: dict[str, dict] = {c["slug"]: c for c in CATEGORIES}

# Default expected return if a goal doesn't set one (a moderate equity-tilted
# assumption, in % per year). Stored per-goal as a percent, like other rate fields.
DEFAULT_RETURN_PCT = 10.0


def category_label(slug: str) -> str:
    cat = CATEGORY_BY_SLUG.get(slug)
    return cat["label"] if cat else (slug or "Other")


def category_color(slug: str) -> str:
    cat = CATEGORY_BY_SLUG.get(slug)
    return cat["color"] if cat else "#8792a0"


def category_icon(slug: str) -> str:
    cat = CATEGORY_BY_SLUG.get(slug)
    return cat["icon"] if cat else "🎯"


def _months_between(start: date, end: date) -> int:
    """Whole months from `start` to `end` (0 if `end` is not in the future)."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


def plan(
    target: float,
    saved: float,
    target_date: date | None,
    annual_return_pct: float | None,
    today: date | None = None,
) -> dict:
    """Work out where a goal stands and what it takes to finish on time.

    Returns a dict with: progress_pct (saved vs target, nominal), months_left,
    projected (what `saved` grows to by the date at the expected return),
    required_monthly (the SIP that closes the remaining gap; None if the date has
    passed and it isn't already funded), required_lumpsum (a one-time amount today
    that, compounding at the same rate, closes the same gap — the "set it aside now"
    alternative to the SIP; same None/0 rules as the SIP), and a status string:

      funded   — projected savings alone reach the target (no new investment needed)
      active   — on the way; contribute `required_monthly` (or `required_lumpsum` now)
      overdue  — target date has passed and it isn't funded
      undated  — no target date, so we can only show progress (no SIP)
    """
    today = today or date.today()
    target = target or 0.0
    saved = saved or 0.0
    progress_pct = min(100.0, (saved / target * 100.0)) if target > 0 else 0.0

    r = (annual_return_pct if annual_return_pct is not None else DEFAULT_RETURN_PCT) / 100.0
    monthly_r = (1.0 + r) ** (1.0 / 12.0) - 1.0

    if target_date is None:
        return {
            "progress_pct": progress_pct, "months_left": None, "projected": saved,
            "required_monthly": None, "required_lumpsum": None, "status": "undated",
        }

    n = _months_between(today, target_date)
    projected = saved * ((1.0 + monthly_r) ** n)
    gap = target - projected

    if gap <= 0:
        status, required, lumpsum = "funded", 0.0, 0.0
    elif n <= 0:
        status, required, lumpsum = "overdue", None, None  # date gone, nothing to plan
    else:
        status = "active"
        if monthly_r == 0:
            required = gap / n
        else:
            # Future value of an ordinary annuity: gap = SIP · ((1+r)^n − 1) / r.
            required = gap * monthly_r / (((1.0 + monthly_r) ** n) - 1.0)
        # One-time amount today that compounds to the gap: L · (1+r)^n = gap.
        lumpsum = gap / ((1.0 + monthly_r) ** n)

    return {
        "progress_pct": progress_pct, "months_left": n, "projected": projected,
        "required_monthly": required, "required_lumpsum": lumpsum, "status": status,
    }
