"""What a house actually costs, and how fast you can be free of it.

Three things this gets right that a bank's EMI calculator doesn't:

1. **Stamp duty and registration come out of your pocket, not the loan.** RBI
   excludes them from the property value when computing loan-to-value (the one
   exception is loans up to ₹10 lakh, where documentation charges may be added).
   So a buyer who has saved "20% for the down payment" is short by another
   5–8% of the price, which in Chennai or Kerala is most of a year's saving.
   This is the single most common miss, and it's the number this page leads with.

2. **RBI caps how much a bank may lend**, by slab, and banks separately cap the
   EMI at a share of income (FOIR). Whichever binds first is your real ceiling.

3. **The prepayment question, asked the way people ask it.** Not "what do I save
   if I pay ₹1 lakh" but "what do I have to pay to be done in ten years". That's
   an inversion, so it's bisected — the same shape as `projection.corpus_requirement`.
"""

from __future__ import annotations

# RBI's loan-to-value ceilings for individual housing loans, by loan slab.
# Banks may lend less; none may lend more.
LTV_BANDS: list[tuple[float, float, str]] = [
    (3_000_000, 0.90, "up to ₹30 lakh"),
    (7_500_000, 0.80, "₹30–75 lakh"),
    (float("inf"), 0.75, "above ₹75 lakh"),
]

# Stamp duty + registration, as a share of the agreement value.
#
# **These are checked against official/industry sources on the date noted and go
# stale.** Rates move with state budgets: Karnataka doubled its registration fee
# from 1% to 2% in August 2025, the first revision since 2003, and this table
# carried the old figure for weeks. Only rows marked `verified` have been
# checked; the rest are shown with a caveat rather than presented as fact.
#
# Several states levy a **cess and surcharge on top of the stamp duty** (not on
# the property value), which is why `stamp`, `cess_pct` and `surcharge_pct` are
# separate: Karnataka's headline 5% is really 5.6% once a 10% cess and a 2%
# urban surcharge are applied to it. A model with only `stamp` and `reg` cannot
# express that, and silently understated the real cost by 1.6 points.
STAMP_DUTY: list[dict] = [
    # state, stamp %, cess (% OF the stamp duty), surcharge (% OF the stamp duty),
    # registration % of value, women's stamp % if lower, verified date
    {"state": "Karnataka (urban/BBMP)", "stamp": 5.0, "cess_pct": 10.0,
     "surcharge_pct": 2.0, "reg": 2.0, "women": None, "verified": "2026-10"},
    {"state": "Maharashtra (Mumbai)", "stamp": 6.0, "cess_pct": 0.0,
     "surcharge_pct": 0.0, "reg": 1.0, "women": 5.0, "verified": None},
    {"state": "Maharashtra (Pune/Nagpur)", "stamp": 7.0, "cess_pct": 0.0,
     "surcharge_pct": 0.0, "reg": 1.0, "women": 6.0, "verified": None},
    {"state": "Delhi", "stamp": 6.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 4.0, "verified": None},
    {"state": "Tamil Nadu", "stamp": 7.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 4.0, "women": None, "verified": None},
    {"state": "Telangana", "stamp": 5.5, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 0.5, "women": None, "verified": None},
    {"state": "Haryana", "stamp": 7.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 5.0, "verified": None},
    {"state": "Uttar Pradesh", "stamp": 7.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 6.0, "verified": None},
    {"state": "Gujarat", "stamp": 4.9, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 4.9, "verified": None},
    {"state": "West Bengal", "stamp": 6.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": None, "verified": None},
    {"state": "Rajasthan", "stamp": 6.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 5.0, "verified": None},
    {"state": "Madhya Pradesh", "stamp": 7.5, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 7.5, "verified": None},
    {"state": "Kerala", "stamp": 8.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 2.0, "women": None, "verified": None},
    {"state": "Punjab", "stamp": 7.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": 5.0, "verified": None},
    {"state": "Andhra Pradesh", "stamp": 5.0, "cess_pct": 0.0, "surcharge_pct": 0.0,
     "reg": 1.0, "women": None, "verified": None},
]


def effective_pct(duty: dict) -> float:
    """Total statutory cost as a share of property value.

    Cess and surcharge apply **to the stamp duty**, not to the property value —
    Karnataka's 5% headline becomes 5.6% before registration is added. Getting
    this wrong understates the cash a buyer needs, which is the one thing this
    page exists to prevent.
    """
    stamp = duty["stamp"]
    loading = stamp * (duty.get("cess_pct", 0.0) + duty.get("surcharge_pct", 0.0)) / 100.0
    return stamp + loading + duty["reg"]


DEFAULT_STATE = "Karnataka (urban/BBMP)"

DEFAULT_RATE_PCT = 8.5        # a typical floating home-loan rate
DEFAULT_YEARS = 20
# Banks cap the EMI at a share of net monthly income (FOIR). 50% is the common
# ceiling for salaried borrowers; lenders tighten it at lower incomes.
DEFAULT_FOIR_PCT = 50.0
# Tenures the prepayment ladder answers for, given a 20-year loan.
TARGET_YEARS = (15, 12, 10, 7, 5)


def duty_for(state: str) -> dict:
    return next((d for d in STAMP_DUTY if d["state"] == state),
                next(d for d in STAMP_DUTY if d["state"] == DEFAULT_STATE))


def max_loan(price: float) -> tuple[float, float, str]:
    """(loan, ltv, band label) under RBI's slabs.

    The slabs are defined on the *loan*, not the price, so it's circular: take
    the most generous band whose resulting loan actually falls inside it.
    """
    for ceiling, ltv, label in LTV_BANDS:
        loan = price * ltv
        if loan <= ceiling:
            return loan, ltv, label
    ceiling, ltv, label = LTV_BANDS[-1]
    return price * ltv, ltv, label


def emi(principal: float, rate_pct: float, years: float) -> float:
    """Standard amortising instalment."""
    n = int(round(years * 12))
    if principal <= 0 or n <= 0:
        return 0.0
    r = rate_pct / 1200.0
    if r == 0:
        return principal / n
    factor = (1 + r) ** n
    return principal * r * factor / (factor - 1)


def schedule(principal: float, rate_pct: float, years: float,
             annual_prepay: float = 0.0, max_months: int | None = None) -> dict:
    """Run the loan month by month. Returns months taken and interest paid.

    The annual lump sum lands after every twelfth payment — which is how people
    actually prepay, out of a bonus, rather than smoothly.
    """
    r = rate_pct / 1200.0
    instalment = emi(principal, rate_pct, years)
    cap = max_months or int(years * 12) + 1200
    balance, interest, months = principal, 0.0, 0
    yearly: list[dict] = []
    year_interest = year_principal = 0.0

    while balance > 0.005 and months < cap:
        due = balance * r
        pay = min(instalment, balance + due)
        balance = balance + due - pay
        interest += due
        year_interest += due
        year_principal += pay - due
        months += 1
        if months % 12 == 0:
            if annual_prepay > 0 and balance > 0:
                lump = min(annual_prepay, balance)
                balance -= lump
                year_principal += lump
            yearly.append({"year": months // 12, "interest": year_interest,
                           "principal": year_principal, "balance": max(balance, 0.0)})
            year_interest = year_principal = 0.0

    if months % 12:
        yearly.append({"year": months // 12 + 1, "interest": year_interest,
                       "principal": year_principal, "balance": max(balance, 0.0)})
    return {"months": months, "interest": interest, "emi": instalment, "yearly": yearly}


def prepay_for_target(principal: float, rate_pct: float, years: float,
                      target_years: float) -> float | None:
    """Annual lump sum that closes the loan in `target_years`. None if impossible.

    Bisected rather than solved: the month loop clamps, rounds to whole months
    and applies the lump sum in discrete jumps, so it doesn't invert cleanly. It
    *is* monotonic in the prepayment amount, which is all bisection needs — the
    same reasoning as projection.corpus_requirement.
    """
    target_months = int(round(target_years * 12))
    if target_months >= int(round(years * 12)):
        return 0.0
    base = schedule(principal, rate_pct, years)
    if base["months"] <= target_months:
        return 0.0
    # An upper bound that certainly clears it: the whole principal in year one.
    lo, hi = 0.0, principal
    if schedule(principal, rate_pct, years, hi)["months"] > target_months:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if schedule(principal, rate_pct, years, mid)["months"] <= target_months:
            hi = mid
        else:
            lo = mid
    return hi


def ladder(principal: float, rate_pct: float, years: float) -> list[dict]:
    """"Pay this much a year and you're done in N" — the page's whole argument."""
    base = schedule(principal, rate_pct, years)
    rows = [{"years": base["months"] / 12.0, "annual": 0.0, "interest": base["interest"],
             "saved": 0.0, "months": base["months"], "base": True}]
    for target in TARGET_YEARS:
        if target * 12 >= base["months"]:
            continue
        annual = prepay_for_target(principal, rate_pct, years, target)
        if annual is None:
            continue
        run = schedule(principal, rate_pct, years, annual)
        rows.append({"years": run["months"] / 12.0, "annual": annual,
                     "interest": run["interest"],
                     "saved": base["interest"] - run["interest"],
                     "months": run["months"], "base": False})
    return rows


def affordable_price(monthly_income: float, rate_pct: float, years: float,
                     foir_pct: float = DEFAULT_FOIR_PCT) -> dict:
    """Work backwards from income: the EMI a bank allows, and the price it buys.

    The binding constraint is whichever is lower — what RBI lets them lend
    against the property, or what your income services.
    """
    budget = monthly_income * foir_pct / 100.0
    if budget <= 0:
        return {"emi": 0.0, "loan": 0.0, "price": 0.0}
    n = int(round(years * 12))
    r = rate_pct / 1200.0
    factor = (1 + r) ** n
    loan = budget * (factor - 1) / (r * factor) if r else budget * n
    # Invert the LTV slab that this loan falls in to get the price it supports.
    price = loan
    for ceiling, ltv, _label in LTV_BANDS:
        if loan <= ceiling:
            price = loan / ltv
            break
    return {"emi": budget, "loan": loan, "price": price}
