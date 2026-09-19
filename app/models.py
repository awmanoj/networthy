"""Data models shared across the parser, storage, and web layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class User:
    """An account. Identified solely by email (login is email + OTP)."""

    email: str
    id: int | None = None
    created_at: datetime | None = None


@dataclass
class Holding:
    """A single line item within a CAS statement (a stock, fund, bond, …)."""

    name: str
    asset_class: str  # an app.classify.AssetClass value ("direct_equity", …)
    isin: str | None = None
    units: float | None = None
    price: float | None = None  # market price / NAV as of the statement date
    value: float | None = None  # market value in INR
    ticker: str | None = None  # exchange ticker from the CAS, e.g. "E2E.NSE" (equities)


@dataclass
class Account:
    """A source account the holdings were grouped under in the CAS.

    One CAS spans several: NSDL/CDSL demat accounts (each a DP + client id),
    mutual fund folios held in statement-of-account form, and — if linked — NPS.
    """

    kind: str  # "demat" | "mutual_fund" | "nps"
    name: str  # display name: DP/broker name, AMC name, or "NPS"
    identifier: str | None = None  # DP ID / Client ID, folio no, or PRAN
    depository: str | None = None  # "NSDL" | "CDSL" for demat accounts
    holdings: list[Holding] = field(default_factory=list)

    @property
    def value(self) -> float:
        return sum(h.value or 0.0 for h in self.holdings)


# How far the parsed rows may drift from the statement's own printed total before
# we say so. Both real parser bugs found so far were catastrophic in size — a
# phantom ₹477 crore holding read off a folio number, and an equity section that
# came back entirely empty — so this does not need to be tight to catch them, and
# a tight threshold that fires on ordinary statements is worse than none: a
# warning nobody believes is a warning nobody reads.
RECONCILE_TOLERANCE = 0.01


@dataclass
class Reconciliation:
    """Do the parsed holdings add up to what the statement says they do?

    The single most dangerous failure mode in this app is not a crash — it's a
    *wrong number shown confidently*. A CAS states its own portfolio total, and
    the per-holding rows are parsed separately, so the two are an independent
    check on each other. Nothing else in the pipeline can catch a parser that
    silently drops a section or invents a value.
    """

    stated: float      # the total printed on the statement
    parsed: float      # the sum of the rows we extracted
    checked: bool      # False when there was nothing to check

    @property
    def delta(self) -> float:
        return self.parsed - self.stated

    @property
    def pct(self) -> float:
        return (self.delta / self.stated * 100.0) if self.stated else 0.0

    @property
    def ok(self) -> bool:
        return not self.checked or abs(self.pct) <= RECONCILE_TOLERANCE * 100.0

    def summary(self) -> str:
        """One line a human can act on. Says which direction it's wrong in,
        because 'missing' and 'invented' need different responses."""
        if self.ok:
            return "Holdings reconcile with the statement total."
        verb = "more than" if self.delta > 0 else "less than"
        return (
            f"The holdings we read add up to ₹{self.parsed:,.0f}, which is "
            f"₹{abs(self.delta):,.0f} {verb} the ₹{self.stated:,.0f} this statement "
            f"says the portfolio is worth ({self.pct:+.1f}%)."
        )


@dataclass
class ParsedStatement:
    """The result of parsing one CAS PDF."""

    statement_date: date
    total_value: float  # total portfolio value in INR (from the CAS summary)
    accounts: list[Account] = field(default_factory=list)
    source_filename: str | None = None

    @property
    def holdings(self) -> list[Holding]:
        """All holdings across every account, flattened."""
        return [h for account in self.accounts for h in account.holdings]

    @property
    def holding_count(self) -> int:
        return len(self.holdings)

    @property
    def parsed_total(self) -> float:
        return sum(h.value or 0.0 for h in self.holdings)

    @property
    def reconciliation(self) -> Reconciliation:
        """Check the parsed rows against the statement's own total.

        `checked` is False when there are no rows to add up: a *summary* CAS
        carries a portfolio total and no per-holding breakdown at all, and
        flagging that as a mismatch would cry wolf on a perfectly good file.
        """
        return Reconciliation(
            stated=self.total_value,
            parsed=self.parsed_total,
            checked=bool(self.holdings),
        )


@dataclass
class Snapshot:
    """A stored net-worth data point, one per statement date."""

    statement_date: date
    total_value: float
    holding_count: int
    source_filename: str | None = None
    id: int | None = None
    user_id: int | None = None
