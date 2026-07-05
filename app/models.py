"""Data models shared across the parser, storage, and web layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Holding:
    """A single line item within a CAS statement (a stock or a fund)."""

    name: str
    kind: str  # "equity" | "mutual_fund" | "bond" | "other"
    isin: str | None = None
    units: float | None = None
    value: float | None = None  # market value in INR


@dataclass
class ParsedStatement:
    """The result of parsing one CAS PDF."""

    statement_date: date
    total_value: float  # total portfolio value in INR
    holdings: list[Holding] = field(default_factory=list)
    source_filename: str | None = None

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
