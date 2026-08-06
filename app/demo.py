"""The public demo account.

`demo@networthyhq.com` is a shared, always-populated account people can explore in
one click from the landing page — no sign-up. Each entry into `/demo` **resets** the
account to this fixture, so however the last visitor poked at it, the next one gets a
clean, coherent portfolio. Values are deliberately fake but realistic; the live-priced
bits (US equity, crypto) really do price live on the server, which shows the feature off.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from . import storage
from .models import Holding, Snapshot

DEMO_EMAIL = "demo@networthyhq.com"

# Per-user tables the fixture writes into — cleared on every reset. `snapshots` is
# last; deleting it cascades to `holdings` (which is keyed by snapshot_id, not user_id).
_TABLES = [
    "property_holdings", "bank_cash", "manual_holdings", "gold_items",
    "alt_investments", "crypto_holdings", "foreign_holdings", "forex_holdings",
    "business_holdings", "liabilities", "expenses", "goals", "networth_holdings",
    "nw_history", "snapshots",
]


def is_demo(user) -> bool:
    return bool(user) and getattr(user, "email", None) == DEMO_EMAIL


def reset(user_id: int) -> None:
    """Wipe the demo account and rebuild it from the fixture below."""
    storage.clear_user_tables(user_id, _TABLES)
    _seed(user_id)


def _seed(user_id: int) -> None:
    u = user_id

    # --- Real estate (the biggest line for most Indian households) ---
    storage.add_property_holding(
        u, "primary-residence", "3BHK · Whitefield", 28_000_000.0,
        cost=19_000_000.0, purchase_date="2019-06-01", notes="Bengaluru · 1,450 sq ft",
    )

    # --- Mutual funds via a CAMS import (fake ISINs → live NAV falls back to these) ---
    storage.replace_networth_import(u, "cams", date(2026, 7, 31), [
        Holding("Parag Parikh Flexi Cap Fund", "mutual_fund", "INF879O01027", 12500.5, 78.34, 1_200_000.0),
        Holding("UTI Nifty 50 Index Fund", "mutual_fund", "INF789F1AUD1", 8200.0, 142.10, 1_165_000.0),
        Holding("Mirae Asset Large Cap Fund", "mutual_fund", "INF769K01101", 5400.0, 95.60, 516_000.0),
        Holding("SBI Small Cap Fund", "mutual_fund", "INF200K01T28", 3100.0, 168.90, 524_000.0),
    ])

    # --- Fixed income (manual) ---
    storage.add_manual_holding(u, "ppf", scheme="SBI PPF", investment_amount=1_850_000.0,
                               rate=7.1, investment_date="2015-04-01", maturity_date="2030-04-01")
    storage.add_manual_holding(u, "epf", scheme="EPFO", investment_amount=2_600_000.0, rate=8.15)
    storage.add_manual_holding(u, "fixed-deposits", scheme="HDFC FD",
                               investment_amount=1_200_000.0, rate=7.4, maturity_date="2027-01-01")

    # --- Bank & cash ---
    storage.add_bank_cash(u, "bank-accounts", 920_000.0, bank_name="HDFC Bank",
                          account_type="Savings", label="Salary")
    storage.add_bank_cash(u, "cash", 60_000.0, label="Cash at home")

    # --- Physical gold (flat value → stable) ---
    storage.add_gold_item(u, description="Coins & jewellery", flat_value=850_000.0)

    # --- Live-priced: US equity + crypto (these actually price live on the server) ---
    storage.add_foreign_holding(u, ticker="AAPL", units=40.0, cost_usd=175.0)
    storage.add_foreign_holding(u, ticker="MSFT", units=15.0, cost_usd=310.0)
    storage.add_crypto_holding(u, symbol="BTC", quantity=0.35, invested_inr=1_800_000.0, label="cold wallet")
    storage.add_crypto_holding(u, symbol="ETH", quantity=3.0, invested_inr=450_000.0)

    # --- Alternate investment ---
    storage.add_alt_investment(u, name="Acme Robotics (angel)", current_value=1_500_000.0,
                               category="Startup / Angel", cost=500_000.0, invested_date="2022-03-01")

    # --- Liability (nets against the assets above) ---
    storage.add_liability(u, "home-loan", lender="HDFC Home Loan", outstanding=3_500_000.0,
                          principal=9_000_000.0, rate=8.6, emi=78_000.0, end_date="2032-06-01")

    # --- Expenses ---
    for name, cat, amt, freq, cnt in [
        ("House rent equivalent", "housing", 45_000, "monthly", 1),
        ("Electricity & internet", "utilities", 6_000, "monthly", 1),
        ("Groceries", "food", 22_000, "monthly", 1),
        ("Fuel & cabs", "transport", 9_000, "monthly", 1),
        ("Health + term premium", "healthcare", 48_000, "annual", 1),
        ("School fees", "education", 120_000, "annual", 2),
        ("Household help", "domestic-help", 18_000, "monthly", 1),
        ("Dining & OTT", "dining", 8_000, "monthly", 1),
        ("Annual vacation", "travel", 250_000, "annual", 1),
    ]:
        storage.add_expense(u, name, cat, float(amt), freq, count=cnt)

    # --- Goals ---
    storage.add_goal(u, "Kids' higher education", "education", 5_000_000.0,
                     saved_amount=1_200_000.0, target_date="2035-06-01", return_pct=11.0, notes="via monthly SIP")
    storage.add_goal(u, "Second home down-payment", "home", 8_000_000.0,
                     saved_amount=5_200_000.0, target_date="2028-04-01")
    storage.add_goal(u, "First ₹1 crore portfolio", "wealth", 10_000_000.0,
                     saved_amount=6_800_000.0, target_date="2030-01-01", return_pct=12.0)
    storage.add_goal(u, "Emergency fund", "emergency", 1_200_000.0, saved_amount=650_000.0)

    # --- NSDL snapshots for the /nsdl-cas chart ---
    for i, (d, v) in enumerate([
        ("2025-09-30", 30_500_000.0), ("2025-12-31", 32_200_000.0),
        ("2026-03-31", 34_100_000.0), ("2026-06-30", 36_900_000.0),
    ]):
        storage.upsert_snapshot(u, Snapshot(
            statement_date=date.fromisoformat(d), total_value=v,
            holding_count=18, source_filename=f"cas_{i}.pdf",
        ))

    # --- Daily net-worth history for the Dashboard trend (~90 days, trending up) ---
    base, target = 33_000_000.0, 36_900_000.0
    for i in range(90, -1, -1):
        day = (date.today() - timedelta(days=i)).isoformat()
        v = base + (target - base) * (90 - i) / 90 + 260_000 * math.sin(i / 6.0)
        storage.record_nw_snapshot(u, day, round(v), round(v), 3_500_000.0, "{}")
