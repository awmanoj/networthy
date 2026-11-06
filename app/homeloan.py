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

Two more, added once people had used it against a real sanction letter:

4. **RBI's slab is a ceiling, not the loan.** A bank applies its LTV to *its own
   valuation*, which is routinely below the agreement value, so a sanction of 65%
   of the price is ordinary — and every rupee the bank doesn't lend is a rupee
   more of cash up front. `loan_amount` takes the sanctioned figure when there is
   one and only falls back to the RBI cap when there isn't.

5. **Closing a loan you already have, with the three levers people actually
   hold**: the EMI they choose to pay, a monthly prepayment to principal, and —
   for an overdraft-style loan (SBI MaxGain, HDFC/BoB "Smart Home", ICICI Money
   Saver) — money parked in the linked account, which earns nothing but is
   charged no interest either. `payoff` runs all three; `payoff_scenarios` shows
   what each does alone, because the question is always "which one matters".
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


def loan_amount(price: float, sanctioned: float | None = None) -> dict:
    """What you will actually borrow — and therefore what you must find in cash.

    RBI's slab caps the bank; it does not oblige it. Lenders apply the ratio to
    the *lower* of the agreement value and their own valuation, and a valuer who
    comes in 10–15% under the price is routine, so a sanction of 65% of what you
    are paying is ordinary. When the reader has a sanction letter, its figure is
    the loan; the slab is reported alongside as what the ceiling would have been.
    A sanction above the price is clamped — nobody is lent more than the asset.
    """
    cap, ltv, label = max_loan(price)
    if sanctioned is None or sanctioned <= 0:
        return {"loan": cap, "ltv": ltv, "band": label, "cap": cap,
                "sanctioned": False, "over_cap": False}
    loan = min(sanctioned, price)
    return {"loan": loan, "ltv": (loan / price) if price > 0 else 0.0, "band": label,
            "cap": cap, "sanctioned": True, "over_cap": loan > cap + 0.5}


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


# --- Closing a loan you already have ----------------------------------------

# How long we are prepared to simulate before calling a loan "never closes".
PAYOFF_MAX_MONTHS = 1200

# The reference loan the server-rendered payoff table is worked through —
# an ordinary mid-life home loan: ₹50 lakh still owed, 15 years to run, with all
# three levers pulled modestly so every row of the table has something to show.
PAYOFF_EXAMPLE = {"balance": 5_000_000.0, "rate_pct": DEFAULT_RATE_PCT, "years_left": 15,
                  "emi_paid": 55_000.0, "monthly_prepay": 10_000.0, "parked": 500_000.0}


def payoff(balance: float, rate_pct: float, emi_paid: float,
           monthly_prepay: float = 0.0, parked: float = 0.0,
           max_months: int = PAYOFF_MAX_MONTHS) -> dict:
    """Months and interest to clear `balance`, with the three levers a borrower holds.

    - `emi_paid` is what actually goes out each month — a stepped-up EMI is the
      quietest way people prepay, and banks let you raise it on request.
    - `monthly_prepay` is an explicit extra transfer to principal every month.
      Arithmetically identical to a higher EMI; kept separate because people
      think of them as different decisions and want to set them separately.
    - `parked` is money sitting in a home-loan **overdraft** account (MaxGain and
      its cousins). It is not repayment — you can take it back — but interest is
      charged only on `balance − parked`, so it behaves like a prepayment that
      stays liquid. The loan is *closed* when the book balance has fallen to the
      parked amount: at that point net outstanding is nil and the parked money
      is simply yours again.

    Interest accrues on the **net** outstanding each month and the payment is
    applied to it, so a parked lump shortens the loan the way a prepayment of the
    same size would. Returns `closes=False` (and `months=None`) when the monthly
    payment doesn't even cover the interest — a loan like that grows rather than
    ends, and saying "never" is more useful than a number in the thousands.
    """
    r = rate_pct / 1200.0
    net_start = max(balance - max(parked, 0.0), 0.0)
    net = net_start
    pay_each = max(emi_paid, 0.0) + max(monthly_prepay, 0.0)
    interest, months = 0.0, 0
    while net > 0.005 and months < max_months:
        due = net * r
        if pay_each <= due:
            return {"months": None, "interest": None, "closes": False, "net_start": net_start}
        pay = min(pay_each, net + due)
        net = net + due - pay
        interest += due
        months += 1
    closes = net <= 0.005
    return {"months": months if closes else None,
            "interest": interest if closes else None,
            "closes": closes, "net_start": net_start}


def payoff_scenarios(balance: float, rate_pct: float, years_left: float,
                     emi_paid: float | None = None, monthly_prepay: float = 0.0,
                     parked: float = 0.0) -> dict:
    """The baseline, each lever on its own, and all three together.

    The baseline is the lender's own schedule — the standard EMI for the
    remaining tenure, nothing extra, nothing parked. Each lever row answers
    "and if I only did this?", which is how people decide which one to pull;
    the combined row is what the headline figure shows. Levers that are off
    (EMI left at the scheduled figure, zero prepay, nothing parked) produce no
    row, so the table only ever lists what the reader actually set.
    """
    sched = emi(balance, rate_pct, years_left)
    paid = sched if emi_paid is None or emi_paid <= 0 else emi_paid
    base = payoff(balance, rate_pct, sched)

    def row(label: str, run: dict) -> dict:
        saved = (base["interest"] - run["interest"]) \
            if run["closes"] and base["closes"] else None
        return {"label": label, "months": run["months"], "interest": run["interest"],
                "saved": saved, "closes": run["closes"]}

    rows = [row("Scheduled EMI, nothing extra", base)]
    emi_changed = abs(paid - sched) > 0.5
    if emi_changed:
        rows.append(row("EMI of this size alone", payoff(balance, rate_pct, paid)))
    if monthly_prepay > 0:
        rows.append(row("Monthly prepayment alone",
                        payoff(balance, rate_pct, sched, monthly_prepay=monthly_prepay)))
    if parked > 0:
        rows.append(row("Parked in the OD account alone",
                        payoff(balance, rate_pct, sched, parked=parked)))
    combined = payoff(balance, rate_pct, paid, monthly_prepay, parked)
    levers = int(emi_changed) + int(monthly_prepay > 0) + int(parked > 0)
    if levers > 1:
        rows.append(row("All of the above together", combined))
    return {"scheduled_emi": sched, "emi_paid": paid, "base": base,
            "combined": combined, "rows": rows, "levers": levers}


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
