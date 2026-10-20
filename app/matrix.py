"""The Plan, one instrument at a time: a year-by-instrument grid.

The Plan page compounds one number (net worth) at one blended return. That is
the right forecast for the questions the app answers in public — does it last,
what would fix it — but it hides the thing most people actually want to argue
with: a property at 6% and an equity portfolio at 12% diverge into two quite
different net worths over twenty years, and a single rate can't show that.

This module is the microscope under the Plan, not a second forecast:

* **It inherits everything from the Plan.** Age, retirement, savings, the
  Expenses burn, inflation, dated goals and windfalls all come from the same
  inputs the Plan reads. Current values per instrument come from the live
  net-worth tree. The only new inputs are a return per row and which row new
  savings land in.
* **With every rate equal to the Plan's and no loans, the total row is the
  Plan's balance, to the rupee.** That's the invariant `test_matrix.py` pins,
  and it's what lets both views exist without contradicting each other: the
  moment a rate is edited the grid diverges, and the page shows the Plan's
  figure beside its own so the gap is visible rather than something to notice.
* **Money has to come from somewhere.** Savings and windfalls land in one
  chosen row (`savings_slug`, default mutual funds). Living costs after
  retirement and goals are drawn in liquidity order — cash first, real estate
  last — so the grid never sells a flat to pay for groceries while an FD sits
  untouched. Without a rule like this the rows would compound in isolation and
  the total would be wrong.
* **Loans pay themselves down on their own schedule**, from the EMI and rate
  already stored against them (or straight-line to the end date when there is
  no EMI, or flat when there's neither). The Plan nets today's outstanding and
  compounds the remainder, so a mortgage is the one place the two views differ
  by design; the page says so.

Nothing here feeds the retire page, the corpus requirement or the digests.
Those stay on the simple model so one assumption drives every public number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import networth, projection

HORIZONS = (10, 20, 30)
DEFAULT_HORIZON = 20


@dataclass(frozen=True)
class Instrument:
    """One row of the grid: a label, and the tree paths whose values it sums."""

    slug: str
    label: str
    paths: tuple[str, ...]
    hint: str = ""


_FA = "assets/financial-assets"
_NFA = "assets/non-financial-assets"

# In **liquidity order** — this is also the order living costs and goals are
# drawn in. Cash goes first and a house goes last; nobody sells a flat to pay
# for groceries while there's money in a fixed deposit.
INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument("bank-cash", "Bank & Cash",
               (f"{_FA}/bank-cash", f"{_FA}/foreign-exchange"),
               "savings accounts, cash, foreign currency"),
    Instrument("fixed-income", "FDs, Bonds & Debt",
               (f"{_FA}/fixed-income/fixed-deposits", f"{_FA}/fixed-income/corporate-bonds",
                f"{_FA}/fixed-income/govt-bonds", f"{_FA}/fixed-income/other-fixed-income"),
               "fixed deposits, bonds, other debt"),
    Instrument("gold-silver", "Gold & Silver",
               (f"{_FA}/gold-silver", f"{_NFA}/physical-gold"),
               "SGBs, gold funds, jewellery"),
    Instrument("mutual-funds", "Mutual Funds", (f"{_FA}/mutual-funds",)),
    Instrument("equity", "Direct Equity", (f"{_FA}/equity",), "Indian stocks"),
    Instrument("foreign-equity", "Foreign / US Equity", (f"{_FA}/foreign-equity",)),
    Instrument("crypto", "Crypto", (f"{_FA}/crypto",)),
    Instrument("others", "Others", (f"{_FA}/others",)),
    # Locked until retirement or maturity, so not where a goal gets paid from
    # even though a CAS files them under fixed income. Drawn on after the
    # market rows, before the genuinely illiquid ones.
    Instrument("retirement-savings", "PPF / EPF / NPS / SSA / NSC",
               (f"{_FA}/fixed-income/ppf", f"{_FA}/fixed-income/epf",
                f"{_FA}/fixed-income/nps", f"{_FA}/fixed-income/sukanya-samriddhi",
                f"{_FA}/fixed-income/nsc"),
               "locked — drawn on only after the rows above"),
    Instrument("alternate-investments", "Startups / Angel / AIF",
               (f"{_FA}/alternate-investments",),
               "illiquid — drawn on only when everything above is gone"),
    Instrument("private-business", "Private Business", (f"{_NFA}/private-business",)),
    Instrument("real-estate", "Real Estate", (f"{_NFA}/real-estate",),
               "drawn on last"),
)
INSTRUMENT_BY_SLUG: dict[str, Instrument] = {i.slug: i for i in INSTRUMENTS}
DRAW_ORDER: tuple[str, ...] = tuple(i.slug for i in INSTRUMENTS)
DEFAULT_SAVINGS_SLUG = "mutual-funds"


@dataclass(frozen=True)
class Loan:
    """A liability row as the grid needs it. `amortise=False` holds it flat —
    a valuation adjustment is a haircut, not a debt being repaid."""

    label: str
    leaf_slug: str
    outstanding: float
    rate_pct: float | None = None
    emi: float | None = None
    end_date: date | None = None
    amortise: bool = True

    @property
    def method(self) -> str:
        """How the grid will walk this loan down, in words, for the page."""
        if not self.amortise:
            return "held flat"
        if self.emi and self.emi > 0:
            return "from its EMI"
        if self.end_date:
            return "straight line to its end date"
        return "held flat — add an EMI or end date to see it reduce"


@dataclass
class MatrixInputs:
    current_age: int
    retire_age: int
    annual_savings: float
    annual_expense: float
    return_pct: float                      # the Plan's blended rate = every row's default
    inflation_pct: float
    values: dict[str, float]               # instrument slug -> value today
    rates: dict[str, float] = field(default_factory=dict)   # slug -> override %
    savings_slug: str = DEFAULT_SAVINGS_SLUG
    outflows: tuple[tuple[int, float, str], ...] = ()
    inflows: tuple[tuple[int, float, str], ...] = ()
    loans: tuple[Loan, ...] = ()
    years: int = DEFAULT_HORIZON

    def rate_for(self, slug: str) -> float:
        r = self.rates.get(slug)
        return self.return_pct if r is None else r


@dataclass
class GridRow:
    slug: str
    label: str
    now: float
    values: list[float]                    # closing at the end of each column year
    rate_pct: float | None = None          # None for loans (they use their own)
    hint: str = ""
    is_savings: bool = False
    method: str = ""                       # loans only


@dataclass
class MatrixResult:
    years: list[int]                       # calendar year each column closes at
    ages: list[int]
    retired: list[bool]
    assets: list[GridRow]
    loans: list[GridRow]
    assets_total: GridRow
    loans_total: GridRow
    net_nominal: list[float]
    net_real: list[float]
    net_now: float
    plan_nominal: list[float]              # the Plan's own balance, same columns
    plan_real: list[float]
    shortfall: list[bool]                  # a draw the assets couldn't meet
    savings: list[float]
    expenses: list[float]
    outflows: list[float]
    inflows: list[float]
    flow_labels: list[tuple[str, ...]]


def project_matrix(p: MatrixInputs, today: date | None = None) -> MatrixResult:
    """Walk every instrument forward a year at a time.

    Within a year, in this order: each row earns its own return; savings and
    any windfall land in the savings row; living costs (after retirement) and
    goals are drawn in liquidity order; loans take a year of repayments. The
    order of the first three steps matches `projection.project`, which is what
    makes the uniform-rate total equal the Plan's balance.
    """
    today = today or date.today()
    infl = p.inflation_pct / 100.0
    horizon = max(1, min(p.years, projection.END_AGE - p.current_age))

    by_offset: dict[int, list[tuple[float, str]]] = {}
    for offset, amount, label in p.outflows:
        by_offset.setdefault(offset, []).append((amount, label))
    in_by_offset: dict[int, list[tuple[float, str]]] = {}
    for offset, amount, label in p.inflows:
        in_by_offset.setdefault(offset, []).append((amount, label))

    vals = {slug: max(0.0, float(p.values.get(slug) or 0.0)) for slug in DRAW_ORDER}
    savings_slug = p.savings_slug if p.savings_slug in vals else DEFAULT_SAVINGS_SLUG
    balances = [max(0.0, float(loan.outstanding or 0.0)) for loan in p.loans]

    asset_series: dict[str, list[float]] = {slug: [] for slug in DRAW_ORDER}
    loan_series: list[list[float]] = [[] for _ in p.loans]
    years, ages, retired_flags, shortfall = [], [], [], []
    savings_col, expense_col, out_col, in_col, labels_col = [], [], [], [], []
    net_nominal, net_real = [], []

    for i in range(horizon):
        age = p.current_age + i
        retired = age >= p.retire_age
        year = today.year + i

        for slug in DRAW_ORDER:
            vals[slug] *= 1 + p.rate_for(slug) / 100.0

        savings = 0.0 if retired else p.annual_savings * (1 + infl) ** i
        expenses = p.annual_expense * (1 + infl) ** i if retired else 0.0
        due = by_offset.get(i, [])
        arriving = in_by_offset.get(i, [])
        outflow = sum(a for a, _ in due)
        inflow = sum(a for a, _ in arriving)

        vals[savings_slug] += savings + inflow

        need = expenses + outflow
        for slug in DRAW_ORDER:
            if need <= 0:
                break
            take = min(vals[slug], need)
            vals[slug] -= take
            need -= take
        shortfall.append(need > 0.5)

        year_end = date(year, 12, 31)
        for k, loan in enumerate(p.loans):
            balances[k] = _loan_after_year(balances[k], loan, year_end)

        for slug in DRAW_ORDER:
            asset_series[slug].append(vals[slug])
        for k in range(len(p.loans)):
            loan_series[k].append(balances[k])

        total_assets = sum(vals.values())
        total_loans = sum(balances)
        net = total_assets - total_loans
        years.append(year)
        ages.append(age)
        retired_flags.append(retired)
        savings_col.append(savings)
        expense_col.append(expenses)
        out_col.append(outflow)
        in_col.append(inflow)
        labels_col.append(tuple(
            [label for _, label in due] + [f"+{label}" for _, label in arriving]
        ))
        net_nominal.append(net)
        net_real.append(net / (1 + infl) ** i)

    assets_now = {slug: max(0.0, float(p.values.get(slug) or 0.0)) for slug in DRAW_ORDER}
    loans_now = [max(0.0, float(loan.outstanding or 0.0)) for loan in p.loans]

    asset_rows = [
        GridRow(
            slug=inst.slug, label=inst.label, now=assets_now[inst.slug],
            values=asset_series[inst.slug], rate_pct=p.rate_for(inst.slug),
            hint=inst.hint, is_savings=inst.slug == savings_slug,
        )
        for inst in INSTRUMENTS
    ]
    loan_rows = [
        GridRow(
            slug=loan.leaf_slug, label=loan.label, now=loans_now[k],
            values=loan_series[k], method=loan.method,
        )
        for k, loan in enumerate(p.loans)
    ]
    assets_total = GridRow(
        slug="assets", label="Total assets", now=sum(assets_now.values()),
        values=[sum(asset_series[s][i] for s in DRAW_ORDER) for i in range(horizon)],
    )
    loans_total = GridRow(
        slug="liabilities", label="Total liabilities", now=sum(loans_now),
        values=[sum(loan_series[k][i] for k in range(len(p.loans))) for i in range(horizon)],
    )

    plan_rows = projection.project(_plan_inputs(p, assets_total.now - loans_total.now,
                                                horizon), today)

    return MatrixResult(
        years=years, ages=ages, retired=retired_flags,
        assets=asset_rows, loans=loan_rows,
        assets_total=assets_total, loans_total=loans_total,
        net_nominal=net_nominal, net_real=net_real,
        net_now=assets_total.now - loans_total.now,
        plan_nominal=[r.closing for r in plan_rows],
        plan_real=[r.real_closing for r in plan_rows],
        shortfall=shortfall,
        savings=savings_col, expenses=expense_col,
        outflows=out_col, inflows=in_col, flow_labels=labels_col,
    )


def _plan_inputs(p: MatrixInputs, corpus: float, horizon: int) -> projection.PlanInputs:
    """The same situation as the Plan page would see it, for the reference row."""
    return projection.PlanInputs(
        current_age=p.current_age, retire_age=p.retire_age,
        annual_savings=p.annual_savings, corpus=corpus,
        annual_expense=p.annual_expense, return_pct=p.return_pct,
        inflation_pct=p.inflation_pct, outflows=p.outflows, inflows=p.inflows,
        end_age=p.current_age + horizon - 1,
    )


def _loan_after_year(balance: float, loan: Loan, year_end: date) -> float:
    """One year of repayments on a loan.

    With an EMI, twelve months of interest-then-payment at the stored rate —
    the way the bank actually computes it. With only an end date, a straight
    line to zero by that date. With neither, the balance is held flat: the grid
    has nothing to go on, and guessing a schedule would hide that. An end date
    always wins: once it has passed, the loan is gone whatever the EMI says.
    """
    if balance <= 0:
        return 0.0
    if not loan.amortise:
        return balance
    if loan.end_date and loan.end_date <= year_end:
        return 0.0
    if loan.emi and loan.emi > 0:
        monthly = (loan.rate_pct or 0.0) / 100.0 / 12.0
        for _ in range(12):
            balance = balance * (1 + monthly) - loan.emi
            if balance <= 0:
                return 0.0
        return balance
    if loan.end_date:
        year_start = date(year_end.year, 1, 1)
        months_left = ((loan.end_date.year - year_start.year) * 12
                       + loan.end_date.month - year_start.month + 1)
        if months_left <= 12:
            return 0.0
        return balance * (1 - 12.0 / months_left)
    return balance


def instrument_values(tree_values: dict[str, float]) -> dict[str, float]:
    """Collapse the rolled-up tree (slug-path -> value) onto the grid's rows."""
    return {
        inst.slug: sum(tree_values.get(path, 0.0) or 0.0 for path in inst.paths)
        for inst in INSTRUMENTS
    }


def loans_from_rows(rows_by_leaf: dict[str, list[dict]]) -> tuple[Loan, ...]:
    """Liability rows from storage, per leaf, as `Loan`s. The valuation
    adjustment leaf is a write-down, not debt, so it never amortises."""
    out: list[Loan] = []
    for leaf, rows in rows_by_leaf.items():
        for r in rows:
            out.append(Loan(
                label=str(r.get("lender") or leaf),
                leaf_slug=leaf,
                outstanding=float(r.get("outstanding") or 0.0),
                rate_pct=r.get("rate"),
                emi=r.get("emi"),
                end_date=_parse_date(r.get("end_date")),
                amortise=leaf != networth.ADJUSTMENT_LEAF,
            ))
    return tuple(out)


def _parse_date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None
