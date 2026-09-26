"""Import holdings from any broker's CSV export.

**One importer, not one per broker.** Zerodha Console, Groww, ICICI Direct,
Trendlyne and Screener all export a sheet, and writing a parser for each is the
CAS trap multiplied: five more things that break silently the next time a vendor
renames a column. Here the user confirms the column mapping, so a format nobody
has ever seen is resolved by a person picking from a dropdown rather than by
shipping code.

The precedent is good. The bank-statement importer's *extraction* read nine real
bank formats exactly, using the same word-root header matching; what failed there
was the categorisation guesswork layered on top, which is why it was deleted.
Holdings have no equivalent guesswork — a row is a name, an ISIN, a quantity and
a value, and the moment there's an ISIN, `classify` and the AMFI NAV lookup
already handle the rest.

Nothing is stored but the result: the file is parsed in memory, the mapping is
confirmed on screen, and only the holdings are written.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .classify import Section, classify
from .models import Holding


class ImportError_(Exception):
    """Raised when a file can't be read as a holdings sheet."""


# What we need, and the word roots that mean it. Order within each tuple is
# preference: "market value" should win over a bare "amount" when both exist.
FIELDS: list[tuple[str, str, tuple[str, ...]]] = [
    ("name",  "Name",     ("name", "scheme", "security", "instrument", "stock",
                           "company", "particular", "description", "symbol")),
    ("isin",  "ISIN",     ("isin",)),
    ("units", "Quantity", ("quantity", "qty", "unit", "share", "holding", "balance")),
    # Roots, not whole names, and truncated to the shared stem: "clos" catches
    # both "Close" and "Previous Closing"; "val" catches Zerodha's "Cur. val".
    # The cost-ish roots sit last and are only reachable on the fallback pass.
    ("price", "Price",    ("ltp", "cmp", "clos", "nav", "price", "rate", "cost", "avg")),
    ("value", "Value",    ("value", "val", "valuation", "worth", "amount")),
]

# Cost-basis columns. A broker sheet usually carries both what you paid and what
# it's worth, and picking the wrong one values the whole portfolio at cost —
# silently, and in a tool whose entire job is what things are worth *now*. These
# columns are only used if nothing else fits, and Zerodha's export is exactly the
# trap: "Average Price" matches "price" and sits to the left of "Previous
# Closing".
_COST_WORDS = ("avg", "average", "buy", "cost", "purchase", "invested", "paid")
FIELD_LABELS = {key: label for key, label, _ in FIELDS}

# A sheet with fewer than this many readable rows is almost certainly the wrong
# file (or the wrong header row), and saying so beats importing two stray lines.
MIN_ROWS = 1
MAX_PREVIEW = 8


def _norm(header: str) -> str:
    """Header reduced to comparable words: "Market Value (INR )" -> "market value"."""
    h = re.sub(r"\(.*?\)", " ", str(header or ""))
    h = re.sub(r"[^a-z0-9 ]+", " ", h.lower())
    return " ".join(h.split())


def to_number(raw) -> float | None:
    """Parse a cell as a number, tolerating Indian grouping and stray symbols."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("₹", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in ("", "-", ".", "-."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _looks_numeric(values: list[str]) -> bool:
    got = [to_number(v) for v in values if str(v).strip()]
    return bool(got) and sum(v is not None for v in got) >= max(1, len(got) * 0.6)


@dataclass
class Sheet:
    """A parsed CSV, ready for the user to confirm how its columns map."""

    headers: list[str]
    rows: list[list[str]]
    mapping: dict[str, int | None] = field(default_factory=dict)

    @property
    def preview(self) -> list[list[str]]:
        return self.rows[:MAX_PREVIEW]


def _find_header_row(table: list[list[str]]) -> int:
    """Index of the row that looks like headers.

    Broker exports routinely open with a title, an account number and a blank
    line before the real table, so the first row is often not the header. The
    header is the first row where at least two cells match something we want.
    """
    best, best_score = 0, 0
    for i, row in enumerate(table[:15]):
        score = sum(
            any(root in _norm(cell) for root in roots)
            for _key, _label, roots in FIELDS
            for cell in row
        )
        if score > best_score:
            best, best_score = i, score
    return best


def suggest_mapping(headers: list[str], rows: list[list[str]]) -> dict[str, int | None]:
    """Guess which column is which. Every guess is shown and editable.

    Matching is on word *roots*, not exact names — ICICI writes "Current Value",
    Zerodha "Cur. val", Groww "Market Value". Matching whole names means a rule
    per broker, forever.
    """
    used: set[int] = set()
    mapping: dict[str, int | None] = {}
    normed = [_norm(h) for h in headers]

    def hunt(key: str, roots: tuple[str, ...], allow_cost: bool) -> int | None:
        for root in roots:                       # preference order within a field
            for i, h in enumerate(normed):
                if i in used or root not in h:
                    continue
                if not allow_cost and any(w in h for w in _COST_WORDS):
                    continue
                # A numeric field whose column holds text is the wrong column.
                if key in ("units", "price", "value"):
                    column = [r[i] for r in rows[:20] if i < len(r)]
                    if not _looks_numeric(column):
                        continue
                return i
        return None

    for key, _label, roots in FIELDS:
        money = key in ("price", "value")
        # Two passes for the money columns: market first, cost only as a
        # fallback. One pass for everything else.
        pick = hunt(key, roots, allow_cost=not money)
        if pick is None and money:
            pick = hunt(key, roots, allow_cost=True)
        if pick is not None:
            used.add(pick)
        mapping[key] = pick
    return mapping


def read_csv(raw: bytes) -> Sheet:
    """Read a CSV export into a Sheet with a suggested mapping."""
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        raise ImportError_("That file isn't readable as text. Export it as CSV.")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel                       # a single-column file sniffs badly
    table = [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    if not table:
        raise ImportError_("That file is empty.")

    start = _find_header_row(table)
    headers = [h.strip() for h in table[start]]
    width = len(headers)
    rows = [r + [""] * (width - len(r)) for r in table[start + 1:]]
    rows = [r[:width] for r in rows]
    if not rows:
        raise ImportError_("Found a header row but no data under it.")
    return Sheet(headers=headers, rows=rows, mapping=suggest_mapping(headers, rows))


def build(sheet: Sheet, mapping: dict[str, int | None]) -> tuple[list[Holding], list[str]]:
    """Turn confirmed columns into holdings. Returns (holdings, skipped reasons).

    A row needs a name and a value. Value may be given directly or derived from
    quantity × price — brokers disagree about which they export, and requiring
    the one they didn't would reject a perfectly good file.
    """
    if mapping.get("name") is None:
        raise ImportError_("Tell us which column holds the holding's name.")
    if mapping.get("value") is None and (
            mapping.get("units") is None or mapping.get("price") is None):
        raise ImportError_(
            "Tell us which column holds the value — or which two hold quantity and price."
        )

    def cell(row: list[str], key: str) -> str:
        i = mapping.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    holdings, skipped = [], []
    for row in sheet.rows:
        name = cell(row, "name")
        if not name or _norm(name) in ("total", "grand total", "sum"):
            continue                              # footer rows, not holdings
        units = to_number(cell(row, "units"))
        price = to_number(cell(row, "price"))
        value = to_number(cell(row, "value"))
        if value is None and units is not None and price is not None:
            value = units * price
        if value is None or value <= 0:
            skipped.append(name)
            continue
        isin = cell(row, "isin").upper() or None
        if isin and not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
            isin = None                           # a malformed ISIN is worse than none
        holdings.append(Holding(
            name=name,
            # section=UNKNOWN on purpose, exactly as the CAMS parser does: it lets
            # the keyword rules run first, so a gold fund lands in Gold rather than
            # defaulting to Mutual Funds and being counted in two leaves.
            asset_class=classify(section=Section.UNKNOWN, isin=isin, description=name),
            isin=isin, units=units, price=price, value=value,
        ))
    return holdings, skipped
