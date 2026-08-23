"""Lifetime cash-flow projection: where today's corpus goes, year by year, to 95.

This is the one part of the app that looks *forward* over a whole life rather
than reporting where things stand. It deliberately reuses what's already
entered instead of asking for it again:

  starting corpus  <- the live net worth (assets - liabilities)
  recurring spend  <- the Expenses annual burn
  one-off outflows <- dated `goals` rows (college, marriage, a house deposit)

so the only genuinely new inputs are the four in `PlanInputs` that nothing else
in the app knows: age, retirement age, what you save a year, and the return you
expect.

Two modelling choices carry the whole thing, and both are deliberate:

* **Before retirement we use savings, after it we use expenses.** `annual_savings`
  is already net of living costs, so subtracting the burn as well during the
  accumulation years would count it twice. At retirement the salary stops, so
  the model switches to drawing the (inflated) burn from the corpus.
* **Goals are pure outflows at their nominal amount.** A goal's target is what
  the user wants *at that date*, so it isn't inflated again. And a goal that
  buys an asset (a house) isn't modelled as acquiring one — v1 treats every
  goal as money leaving. The UI says so; the workaround is to enter the deposit
  as the goal and the EMI under recurring expenses.

Everything is deterministic. Rather than pretend a single line is the future,
`project_band()` runs the same model at three return assumptions so the output
reads as a range. No tax is modelled anywhere — which makes every number here
somewhat optimistic, and the page says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

END_AGE = 95              # how far the projection runs
BAND_DELTA_PCT = 2.0      # the +/- return spread the optimistic/pessimistic lines use

DEFAULT_RETURN_PCT = 10.0
DEFAULT_INFLATION_PCT = 6.0   # India-realistic general inflation, not the US ~3%
DEFAULT_RETIRE_AGE = 60


@dataclass
class PlanInputs:
    """Everything the projection needs. Amounts are INR, rates are percents."""

    current_age: int
    retire_age: int
    annual_savings: float          # what you add each year while working
    corpus: float                  # today's net worth (assets - liabilities)
    annual_expense: float          # today's recurring burn, from the Expenses page
    return_pct: float = DEFAULT_RETURN_PCT
    inflation_pct: float = DEFAULT_INFLATION_PCT
    # (year_offset_from_now, amount, label) — dated goals, resolved by the caller.
    outflows: tuple[tuple[int, float, str], ...] = ()
    end_age: int = END_AGE


@dataclass
class YearPoint:
    """One row of the projection."""

    age: int
    year: int
    opening: float
    growth: float
    savings: float
    expenses: float
    outflows: float
    outflow_labels: tuple[str, ...]
    closing: float
    # `closing` deflated back to today's rupees. Nominal balances decades out are
    # arithmetically right and humanly meaningless — a 10% return against 6%
    # inflation turns a ₹3 cr corpus into "₹581 crore at 95", which reads as a
    # bug. Everything user-facing shows this instead.
    real_closing: float
    retired: bool


def project(p: PlanInputs, today: date | None = None) -> list[YearPoint]:
    """Walk the corpus forward one year at a time.

    Within a year: the opening balance earns the return, then savings land and
    outflows are taken. Treating flows as end-of-year is the conservative
    reading (a year's savings earns nothing in the year it's earned) and it
    keeps the arithmetic explainable, which matters more here than precision
    the inputs don't justify.
    """
    today = today or date.today()
    r = p.return_pct / 100.0
    infl = p.inflation_pct / 100.0

    by_offset: dict[int, list[tuple[float, str]]] = {}
    for offset, amount, label in p.outflows:
        by_offset.setdefault(offset, []).append((amount, label))

    rows: list[YearPoint] = []
    balance = float(p.corpus)

    for i, age in enumerate(range(p.current_age, p.end_age + 1)):
        retired = age >= p.retire_age
        opening = balance

        growth = opening * r
        # Salary and living costs both track inflation, so the savings you add
        # and the burn you draw are stated in that year's rupees.
        savings = 0.0 if retired else p.annual_savings * (1 + infl) ** i
        expenses = p.annual_expense * (1 + infl) ** i if retired else 0.0

        due = by_offset.get(i, [])
        outflow_total = sum(a for a, _ in due)

        closing = opening + growth + savings - expenses - outflow_total
        if closing <= 0:
            closing = 0.0

        rows.append(YearPoint(
            age=age,
            year=today.year + i,
            opening=opening,
            growth=growth,
            savings=savings,
            expenses=expenses,
            outflows=outflow_total,
            outflow_labels=tuple(label for _, label in due),
            closing=closing,
            real_closing=closing / (1 + infl) ** i,
            retired=retired,
        ))

        # Once it's gone it stays gone: a zero balance earns no return, so the
        # remaining years just show how long the shortfall runs. (Still-working
        # years can rebuild it — savings keep landing.)
        balance = closing

    return rows


def depletion_age(rows: list[YearPoint]) -> int | None:
    """The age at which the corpus first hits zero, or None if it never does."""
    for row in rows:
        if row.closing <= 0:
            return row.age
    return None


def summarise(rows: list[YearPoint], p: PlanInputs) -> dict:
    """Headline numbers for the page: does it last, and what's left."""
    gone_at = depletion_age(rows)
    infl = p.inflation_pct / 100.0
    retire_row = next((row for row in rows if row.retired), None)
    at_retirement = retire_row.opening if retire_row else (rows[-1].closing if rows else 0.0)
    # Deflate the retirement corpus by the years until it, so it's comparable to
    # today's net worth rather than a big number in future rupees.
    years_to_retire = (retire_row.age - rows[0].age) if retire_row and rows else 0
    return {
        "lasts": gone_at is None,
        "depletion_age": gone_at,
        "corpus_at_retirement": at_retirement,
        "corpus_at_retirement_real": at_retirement / (1 + infl) ** years_to_retire,
        "final_corpus": rows[-1].closing if rows else 0.0,
        "final_corpus_real": rows[-1].real_closing if rows else 0.0,
        "total_outflows": sum(row.outflows for row in rows),
        "years_projected": len(rows),
        "end_age": p.end_age,
    }


def project_band(p: PlanInputs, today: date | None = None,
                 delta_pct: float = BAND_DELTA_PCT) -> dict:
    """The same projection at three return assumptions.

    A single line to 95 reads as a forecast, which it isn't — the return
    assumption dominates everything and nobody knows it. Running the model at
    +/- `delta_pct` and shading between is the cheapest honest presentation:
    three loops, and the output reads as a range instead of a promise.
    """
    def at(return_pct: float) -> list[YearPoint]:
        return project(_with_return(p, return_pct), today)

    base = at(p.return_pct)
    low = at(max(0.0, p.return_pct - delta_pct))
    high = at(p.return_pct + delta_pct)
    lo_pct = max(0.0, p.return_pct - delta_pct)
    return {
        "base": base,
        "low": low,
        "high": high,
        "summary": summarise(base, p),
        "low_summary": summarise(low, p),
        "high_summary": summarise(high, p),
        # What it would take to reach end_age at each return assumption — the
        # spread between these is usually the most decision-useful thing here.
        "need": corpus_requirement(p, today),
        "need_low": corpus_requirement(_with_return(p, lo_pct), today),
        "need_high": corpus_requirement(_with_return(p, p.return_pct + delta_pct), today),
        "delta_pct": delta_pct,
    }


def years_corpus_lasts(corpus: float, annual_expense: float,
                       return_pct: float = DEFAULT_RETURN_PCT,
                       inflation_pct: float = DEFAULT_INFLATION_PCT,
                       max_years: int = END_AGE) -> int | None:
    """How many years a corpus survives being drawn on, or None if it outlives
    `max_years`.

    This is the SWP question — "I stop earning today and withdraw my expenses,
    when does it run out?" — so it reuses the same year loop the Plan page runs
    rather than a second implementation: no savings, retired from year one, and
    the draw inflating each year. That last part is what most withdrawal
    calculators leave out, and it's the whole story: at 6% inflation the amount
    you withdraw doubles every twelve years.
    """
    p = PlanInputs(
        current_age=0, retire_age=0, annual_savings=0.0, corpus=corpus,
        annual_expense=annual_expense, return_pct=return_pct,
        inflation_pct=inflation_pct, end_age=max_years,
    )
    # With current_age 0, a row's `age` is just the number of years elapsed.
    return depletion_age(project(p))


def corpus_requirement(p: PlanInputs, today: date | None = None,
                       tolerance: float = 1_000.0) -> dict:
    """How much you'd need **today** for the plan to reach `end_age`.

    Answers the question the depletion age raises but doesn't settle: "runs out
    at 75" is a diagnosis, "you're ₹94 lakh short today" is something you can act
    on. Returns the same shape whichever side of the line you're on — a negative
    `gap` is the cushion you're carrying above the minimum.

    Solved by bisection rather than algebra: the year loop clamps at zero, mixes
    inflating flows with fixed-date outflows, and switches regime at retirement,
    none of which invert cleanly. It is monotonic in the starting corpus though
    (more money is never worse), which is all bisection needs. Each probe is a
    56-year loop, so a solve is a few thousand operations.
    """
    today = today or date.today()

    def lasts(corpus: float) -> bool:
        return depletion_age(project(_with_corpus(p, corpus), today)) is None

    if lasts(0.0):
        # Savings alone carry the plan — no starting corpus is required at all.
        return {"needed": 0.0, "gap": -p.corpus, "lasts": True}

    # Bracket the answer: double until the plan survives. Bounded by construction
    # (a finite horizon is always satisfiable with enough money), but capped so a
    # pathological input can't spin.
    hi = max(p.corpus, abs(p.annual_expense), 1.0)
    for _ in range(200):
        if lasts(hi):
            break
        hi *= 2
    else:                                     # pragma: no cover - unreachable
        return {"needed": float("inf"), "gap": float("inf"), "lasts": False}

    lo = 0.0
    while hi - lo > tolerance:
        mid = (lo + hi) / 2
        if lasts(mid):
            hi = mid
        else:
            lo = mid

    return {"needed": hi, "gap": hi - p.corpus, "lasts": lasts(p.corpus)}


def _with_corpus(p: PlanInputs, corpus: float) -> PlanInputs:
    return PlanInputs(
        current_age=p.current_age, retire_age=p.retire_age,
        annual_savings=p.annual_savings, corpus=corpus,
        annual_expense=p.annual_expense, return_pct=p.return_pct,
        inflation_pct=p.inflation_pct, outflows=p.outflows, end_age=p.end_age,
    )


def _with_return(p: PlanInputs, return_pct: float) -> PlanInputs:
    return PlanInputs(
        current_age=p.current_age, retire_age=p.retire_age,
        annual_savings=p.annual_savings, corpus=p.corpus,
        annual_expense=p.annual_expense, return_pct=return_pct,
        inflation_pct=p.inflation_pct, outflows=p.outflows, end_age=p.end_age,
    )


def outflows_from_goals(goals: list[dict], today: date | None = None,
                        end_age: int = END_AGE,
                        current_age: int = 0) -> tuple[tuple[int, float, str], ...]:
    """Turn dated goal rows into (year_offset, amount, label) outflows.

    Undated goals are skipped — without a date there's no year to spend them in.
    Goals already in the past are skipped too: the projection starts today, and
    a target date that has passed says nothing about future cash flow.
    """
    today = today or date.today()
    horizon = end_age - current_age
    out: list[tuple[int, float, str]] = []
    for g in goals:
        raw = g.get("target_date")
        if not raw:
            continue
        try:
            when = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        offset = when.year - today.year
        if offset < 0 or offset > horizon:
            continue
        amount = float(g.get("target_amount") or 0.0)
        if amount <= 0:
            continue
        out.append((offset, amount, str(g.get("name") or "Goal")))
    return tuple(sorted(out))
