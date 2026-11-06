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
   if I pay ₹1 lakh" but "what do I have to pay a month to be done in ten years".
   That's an inversion, so it's bisected — the same shape as `projection.corpus_requirement`.

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

**There is one loan and one month loop.** A new loan is an existing loan whose
balance is the whole principal and whose tenure is the whole tenure, so the
buying half of the page and the already-have-a-loan half feed the same `payoff`
run: the ladder ("extra per month to finish in N years", `ladder`) is bisected
on it, the lever table is read off it, and the year-by-year schedule it returns
is the one the page shows. An earlier version kept a second loop for annual
lump-sum prepayments; two loops meant two models that could not share inputs
and quietly disagreed on the same loan.
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
# Tenures the prepayment ladder answers for (those shorter than the loan).
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


# --- Closing a loan you already have ----------------------------------------

# How long we are prepared to simulate before calling a loan "never closes".
PAYOFF_MAX_MONTHS = 1200

# The reference loan the server-rendered tables are worked through: the page's
# own default state — the ₹1 crore buyer's RBI-cap loan, scheduled EMI, nothing
# extra, nothing parked — so what a crawler reads is what a visitor first sees.
PAYOFF_EXAMPLE = {"balance": 7_500_000.0, "rate_pct": DEFAULT_RATE_PCT, "years_left": DEFAULT_YEARS,
                  "emi_paid": None, "monthly_prepay": 0.0, "parked": 0.0}


def payoff(balance: float, rate_pct: float, emi_paid: float,
           monthly_prepay: float = 0.0, parked: float = 0.0,
           max_months: int = PAYOFF_MAX_MONTHS) -> dict:
    """Months, interest and a year-by-year schedule to clear `balance`, with the
    three levers a borrower holds.

    - `emi_paid` is what actually goes out each month — a stepped-up EMI is the
      quietest way people prepay, and banks let you raise it on request.
    - `monthly_prepay` is an explicit extra transfer to principal every month.
      Arithmetically identical to a higher EMI; kept separate because people
      think of them as different decisions and want to set them separately.
    - `parked` is money sitting in a home-loan **overdraft** account (MaxGain and
      its cousins). It is not repayment — you can take it back — but interest is
      charged only on `balance − parked`, so it behaves like a prepayment that
      stays liquid. It is modelled as held flat for the life of the loan; money
      added to it every month is `monthly_prepay`. The loan is *closed* when the
      book balance has fallen to the parked amount: net outstanding is nil and
      the parked money is simply yours again.

    Interest accrues on the **net** outstanding each month and the payment is
    applied to it. `yearly` carries one row per (possibly partial) year — opening
    and closing *book* balance, what was paid, how much of it was interest, and
    the net balance after what's parked — which is the page's payment schedule.
    Returns `closes=False` (and `months=None`) when the monthly payment doesn't
    even cover the interest — a loan like that grows rather than ends, and
    saying "never" is more useful than a number in the thousands.
    """
    r = rate_pct / 1200.0
    parked = max(parked, 0.0)
    net_start = max(balance - parked, 0.0)
    net = net_start
    pay_each = max(emi_paid, 0.0) + max(monthly_prepay, 0.0)
    interest, months = 0.0, 0
    yearly: list[dict] = []
    y_open, y_paid, y_int = net + parked, 0.0, 0.0

    def close_year() -> None:
        yearly.append({"year": len(yearly) + 1, "opening": y_open, "paid": y_paid,
                       "interest": y_int, "principal": y_paid - y_int,
                       "closing": net + parked, "net": net})

    while net > 0.005 and months < max_months:
        due = net * r
        if pay_each <= due:
            return {"months": None, "interest": None, "closes": False,
                    "net_start": net_start, "yearly": []}
        pay = min(pay_each, net + due)
        net = net + due - pay
        interest += due
        y_paid += pay
        y_int += due
        months += 1
        if months % 12 == 0:
            close_year()
            y_open, y_paid, y_int = net + parked, 0.0, 0.0
    if months % 12:
        close_year()
    closes = net <= 0.005
    return {"months": months if closes else None,
            "interest": interest if closes else None,
            "closes": closes, "net_start": net_start, "yearly": yearly}


def prepay_for_target(balance: float, rate_pct: float, emi_paid: float,
                      target_years: float, parked: float = 0.0) -> float | None:
    """Extra per month, on top of `emi_paid`, that closes the loan within
    `target_years`. 0.0 if it already does; None if nothing can.

    Bisected rather than solved: the month loop clamps and rounds to whole
    months, so it doesn't invert cleanly. It *is* monotonic in the prepayment,
    which is all bisection needs — the same reasoning as
    projection.corpus_requirement.
    """
    target_months = int(round(target_years * 12))
    base = payoff(balance, rate_pct, emi_paid, 0.0, parked)
    if base["closes"] and base["months"] <= target_months:
        return 0.0
    # An upper bound that certainly clears it: the whole balance in month one.
    lo, hi = 0.0, max(balance, 1.0)
    top = payoff(balance, rate_pct, emi_paid, hi, parked)
    if not top["closes"] or top["months"] > target_months:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        run = payoff(balance, rate_pct, emi_paid, mid, parked)
        if run["closes"] and run["months"] <= target_months:
            hi = mid
        else:
            lo = mid
    return hi


def ladder(balance: float, rate_pct: float, emi_paid: float,
           parked: float = 0.0) -> list[dict]:
    """"Pay this much extra a month and you're done in N" — the page's argument.

    Runs on the same loop as everything else, so it respects the EMI actually
    paid and whatever is parked. The base row is that loan with nothing extra.
    """
    base = payoff(balance, rate_pct, emi_paid, 0.0, parked)
    if not base["closes"]:
        return []
    rows = [{"years": base["months"] / 12.0, "monthly": 0.0, "interest": base["interest"],
             "saved": 0.0, "months": base["months"], "base": True}]
    for target in TARGET_YEARS:
        if target * 12 >= base["months"]:
            continue
        monthly = prepay_for_target(balance, rate_pct, emi_paid, target, parked)
        if monthly is None:
            continue
        run = payoff(balance, rate_pct, emi_paid, monthly, parked)
        rows.append({"years": run["months"] / 12.0, "monthly": monthly,
                     "interest": run["interest"],
                     "saved": base["interest"] - run["interest"],
                     "months": run["months"], "base": False})
    return rows


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
