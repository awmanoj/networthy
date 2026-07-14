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


@dataclass
class Snapshot:
    """A stored net-worth data point, one per statement date."""

    statement_date: date
    total_value: float
    holding_count: int
    source_filename: str | None = None
    id: int | None = None
    user_id: int | None = None
