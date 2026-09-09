"""What return would it take to get from here to a target net worth?

The other public tools answer "where do I stand" and "how much do I need". This
one answers the question that follows: *given what I have and what I can put
away, what does my portfolio have to earn?*

Solving for the **return** rather than the years or the amount is the point.
Everyone can name a target and a date; almost nobody checks whether the rate
that implies is one any portfolio actually delivers. Stating it as a number —
and then saying plainly that 18% a year is not a plan — is the useful part.

The second half is the bit only a tracker can do: compare that required return
against what the user's *actual asset mix* would plausibly earn. A 60%-savings-
account portfolio needing 14% a year isn't short of ambition, it's short of
equity, and that's a different conversation.
"""

from __future__ import annotations

from dataclasses import dataclass

# Long-run nominal INR expectations per asset class, before tax and costs.
# Deliberately unexciting: these are the numbers you'd defend to a sceptic, not
# the ones a fund brochure prints. They exist to answer "is the required return
# plausible for *this* portfolio", so being a point conservative is the safer
# error — it flags a stretch as a stretch.
ASSET_RETURNS: dict[str, float] = {
    "Equity": 12.0,               # Indian direct equity
    "Mutual Funds": 11.0,         # blended equity/hybrid
    "Foreign / US Equity": 10.0,  # in INR terms, net of currency drift
    "Crypto": 12.0,               # not a forecast — a placeholder for "high, unknowable"
    "Fixed Income": 7.0,          # PPF/EPF/FD/bonds
    "Gold & Silver": 8.0,
    "Physical Gold & Jewellery": 8.0,
    "Bank Account & Cash": 4.0,
    "Foreign Exchange": 3.0,
    "Real Estate": 7.0,           # capital appreciation; rent is separate
    "Private Business": 12.0,
    "Alternate Investments": 12.0,
    "Others": 6.0,
}
DEFAULT_ASSET_RETURN = 7.0

# How to read a required return. The bands are judgements, not maths, and the
# page says so — but leaving the reader to interpret "13.4%" unaided is how
# these calculators end up flattering people.
VERDICTS: list[tuple[float, str, str]] = [
    (6.0, "comfortable",
     "A fixed deposit could do this. You don't need to take equity risk to get there."),
    (9.0, "reasonable",
     "A balanced portfolio has historically cleared this. No heroics required."),
    (12.0, "demanding",
     "Equity-heavy, and it has to work for the whole period. Historically achievable, "
     "but not something to assume."),
    (15.0, "a stretch",
     "Above what a diversified Indian portfolio has reliably delivered over long "
     "periods. Possible, but you'd be relying on things going well."),
    (float("inf"), "not a plan",
     "No ordinary portfolio compounds at this rate for years on end. Treat this as "
     "the arithmetic telling you the target, the timeline or the savings has to move."),
]


@dataclass
class PathInputs:
    current: float          # net worth today
    target: float           # where you want to get to
    years: int
    annual_savings: float = 0.0   # added at the end of each year


def future_value(current: float, annual_savings: float, years: int,
                 return_pct: float) -> float:
    """Corpus after `years`, compounding and adding savings at each year end."""
    r = return_pct / 100.0
    balance = current
    for _ in range(years):
        balance = balance * (1 + r) + annual_savings
    return balance


def required_return(p: PathInputs) -> float | None:
    """The annual return that turns `current` into `target` in `years`.

    Bisected rather than solved: with savings in the mix there's no closed form
    for the rate, and the function is monotonic in it, which is all bisection
    needs. Returns None when the target is unreachable at any sane rate — the
    honest answer to "I have ₹5 lakh and want ₹100 crore in 3 years".
    """
    if p.years <= 0 or p.target <= 0:
        return None
    # Already there, or savings alone suffice: no return needed.
    if future_value(p.current, p.annual_savings, p.years, 0.0) >= p.target:
        return 0.0

    lo, hi = 0.0, 100.0
    if future_value(p.current, p.annual_savings, p.years, hi) < p.target:
        return None                      # not reachable even at 100% a year
    for _ in range(200):
        mid = (lo + hi) / 2
        if future_value(p.current, p.annual_savings, p.years, mid) < p.target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return hi


def verdict(return_pct: float) -> tuple[str, str]:
    """(label, explanation) for a required return."""
    for ceiling, label, note in VERDICTS:
        if return_pct < ceiling:
            return label, note
    return VERDICTS[-1][1], VERDICTS[-1][2]


def blended_return(buckets: list[dict]) -> float | None:
    """What a portfolio of these allocations would plausibly earn, weighted.

    `buckets` are the dashboard's allocation rows ({'label', 'value'}). Returns
    None when there's nothing to weigh.
    """
    total = sum(b.get("value") or 0.0 for b in buckets)
    if total <= 0:
        return None
    weighted = sum(
        (b.get("value") or 0.0) * ASSET_RETURNS.get(b.get("label"), DEFAULT_ASSET_RETURN)
        for b in buckets
    )
    return weighted / total


def years_to_target(p: PathInputs, return_pct: float, cap: int = 60) -> int | None:
    """Years to reach the target at a given return, or None if not within `cap`."""
    if p.target <= p.current:
        return 0
    r = return_pct / 100.0
    balance = p.current
    for year in range(1, cap + 1):
        balance = balance * (1 + r) + p.annual_savings
        if balance >= p.target:
            return year
    return None


def savings_needed(p: PathInputs, return_pct: float) -> float | None:
    """Annual saving required to hit the target at a given return.

    The lever people actually control. Closed form: the target less what today's
    corpus grows to, divided by the future value of a ₹1 annuity.
    """
    if p.years <= 0:
        return None
    r = return_pct / 100.0
    grown = p.current * (1 + r) ** p.years
    if grown >= p.target:
        return 0.0
    factor = p.years if r == 0 else ((1 + r) ** p.years - 1) / r
    return (p.target - grown) / factor
