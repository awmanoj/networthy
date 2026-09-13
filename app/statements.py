"""Read a bank statement, and turn its debits into an expense picture.

The Expenses page asks people to recall what they spend, which is the part they
get wrong — small recurring things get forgotten and annual ones get missed
entirely. A statement already knows. This module reads one, sorts the debits
into the same categories the page uses, annualises them, and hands back a
*suggestion* the user reviews before anything is saved.

Two decisions shape everything here:

* **Transactions are never stored.** The statement is parsed in memory, the user
  reviews what it found, and only the resulting expense rows are written. The
  document and the line items are discarded. A bank statement is more revealing
  than a CAS — counterparties, habits, who you pay — so the safest design is to
  extract the shape and forget the detail.

* **Exclusions matter more than categories.** Most of a current account's debits
  are not expenses: money moved to your own accounts, a credit-card bill (the
  card's purchases are the spending, the payment is a transfer), SIPs and other
  investments, and loan EMIs, which this app deliberately keeps under
  Liabilities. Counting those turns a useful number into a wrong one, and wrong
  in the direction of alarming people.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime


class StatementParseError(Exception):
    """Raised when a file can't be read as a bank statement."""


@dataclass
class Txn:
    """One line of a statement. Held in memory only — never persisted."""

    when: date
    narration: str
    debit: float = 0.0
    credit: float = 0.0


# --- Exclusions -------------------------------------------------------------
#
# Checked before categorisation, because a wrongly *included* debit is worse
# than a wrongly categorised one: it inflates the total, and the total is the
# number people act on.

# Money leaving for your own benefit rather than being spent.
_EXCLUDE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("self-transfer", re.compile(
        r"\b(self|own\s+a/?c|to\s+own|funds?\s+transfer|imps.*self|"
        r"transfer\s+to\s+(own|self))\b", re.I)),
    # A card bill is settling purchases already made; the purchases are the
    # spending. Counting both is the single biggest way to double a burn figure.
    ("credit-card bill", re.compile(
        r"\b(credit\s*card\s*(bill|payment|pmt)|cc\s*(bill|pmt)|autopay.*card|"
        r"card\s*payment|amex.*payment|billdesk.*card)\b", re.I)),
    # Investments are savings, not spending.
    ("investment", re.compile(
        r"\b(sip|mutual\s*fund|mf\s*purchase|nach.*(mf|mutual)|zerodha|groww|"
        r"upstox|kuvera|coin\s*by|icicidirect|hdfcsec|nse\s*clearing|"
        r"bse\s*ltd|ppf|nps|sukanya|elss|rd\s*instal)\b", re.I)),
    # EMIs live under Liabilities in this app, on purpose — counting them here
    # too would make the burn-rate and net-worth views disagree.
    ("loan EMI", re.compile(
        r"\b(emi|loan\s*(repay|instal|emi)|home\s*loan|car\s*loan|"
        r"personal\s*loan|hdfc\s*ltd\s*emi)\b", re.I)),
    # Moving money to a spouse/parent's account, or to a broker wallet.
    ("wallet top-up", re.compile(
        r"\b(add(ed)?\s+to\s+wallet|wallet\s+load|paytm\s+wallet\s+add)\b", re.I)),
]

# --- Categorisation ---------------------------------------------------------
#
# Ordered, first match wins. Merchant names beat generic words, so a grocery
# delivery is food rather than "online shopping". Tuned for Indian statements,
# where the narration is usually UPI/<vpa>/<merchant> or NEFT/<name>.
_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(rent|landlord|society\s*maint|maintenance\s*charge|"
                r"housing\s*society|apartment\s*assoc)\b", re.I), "housing"),
    (re.compile(r"\b(electricity|bescom|msedcl|tneb|adani\s*elec|torrent\s*power|"
                r"gas\s*bill|indane|hp\s*gas|water\s*bill|broadband|airtel|jio|"
                r"vodafone|vi\s*postpaid|act\s*fibernet|hathway|tata\s*play|"
                r"dish\s*tv)\b", re.I), "utilities"),
    (re.compile(r"\b(bigbasket|dmart|blinkit|zepto|instamart|grofers|jiomart|"
                r"more\s*retail|reliance\s*fresh|spencer|nature'?s\s*basket|"
                r"milk|amul|country\s*delight|kirana|grocer)\b", re.I), "food"),
    (re.compile(r"\b(swiggy|zomato|dominos|pizza|starbucks|cafe|restaurant|"
                r"eatery|dineout|eazydiner|bookmyshow|pvr|inox|netflix|"
                r"hotstar|spotify|prime\s*video|sony\s*liv|zee5)\b", re.I), "dining"),
    (re.compile(r"\b(uber|ola|rapido|irctc|metro\s*rail|fastag|iocl|hpcl|bpcl|"
                r"indian\s*oil|bharat\s*petro|hp\s*petrol|fuel|petrol|diesel|"
                r"parking|toll)\b", re.I), "transport"),
    (re.compile(r"\b(hospital|clinic|apollo|fortis|max\s*health|medplus|"
                r"pharmeasy|1mg|netmeds|pharmacy|chemist|diagnostic|lab\s*test|"
                r"dr\.|doctor|life\s*insur|health\s*insur|term\s*plan|mediclaim|"
                r"star\s*health|niva\s*bupa|hdfc\s*life|icici\s*pru|lic\s*of\s*india|"
                r"max\s*life|policy\s*premium|premium)\b", re.I), "healthcare"),
    (re.compile(r"\b(school|college|tuition|coaching|byju|unacademy|vedantu|"
                r"cuemath|kumon|daycare|creche|playschool|academy|university|"
                r"exam\s*fee|term\s*fee)\b", re.I), "education"),
    (re.compile(r"\b(maid|cook|driver|nanny|housekeep|domestic|servant|"
                r"urban\s*company|urbanclap)\b", re.I), "domestic-help"),
    (re.compile(r"\b(makemytrip|goibibo|cleartrip|yatra|airbnb|oyo|indigo|"
                r"vistara|air\s*india|spicejet|akasa|booking\.com|agoda|"
                r"hotel|resort|travel)\b", re.I), "travel"),
    (re.compile(r"\b(donation|temple|trust|ngo|charity|gift|giftcard|"
                r"tanishq|kalyan\s*jewel|jeweller)\b", re.I), "gifting"),
    (re.compile(r"\b(amazon|flipkart|myntra|ajio|nykaa|meesho|tatacliq|"
                r"decathlon|lifestyle|shoppers\s*stop|zara|h&m|uniqlo|croma|"
                r"reliance\s*digital|salon|spa|gym|cult\.?fit|barber)\b",
                re.I), "lifestyle"),
]
DEFAULT_CATEGORY = "other"

# What a salary credit looks like. Kept narrow: a stray large credit is more
# likely a refund or a transfer, and calling that "income" would be worse than
# saying nothing.
_SALARY_RE = re.compile(
    r"\b(salary|sal\s*cr|sal\s*for|payroll|monthly\s*sal|"
    r"neft.*salary|imps.*salary|remuneration)\b", re.I)


def classify_txn(narration: str) -> tuple[str | None, str | None]:
    """(category, excluded_reason) for one narration.

    Exactly one is non-None: a transaction is either an expense in some
    category, or excluded with a reason the UI can show — "we left this out and
    here's why" is far more trustworthy than a number that silently omits things.
    """
    for reason, pattern in _EXCLUDE_RULES:
        if pattern.search(narration):
            return None, reason
    for pattern, category in _CATEGORY_RULES:
        if pattern.search(narration):
            return category, None
    return DEFAULT_CATEGORY, None


def is_salary(narration: str) -> bool:
    return bool(_SALARY_RE.search(narration))


# --- Reading the file -------------------------------------------------------
#
# CSV first, because every Indian bank offers it and the columns are
# unambiguous. PDF is best-effort: layouts differ per bank and, as the CAS work
# showed, a wrong number read confidently is worse than no number at all — so
# the PDF path is deliberately conservative and says when it found little.

_DATE_FORMATS = ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
                 "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%d %B %Y")

# Header matching is by *word root*, not exact string. Every bank names these
# columns differently and decorates them — "Withdrawal Amount (INR )",
# "Withdrawals", "Withdrawal Amt.", "DR" — so matching whole names means adding a
# rule per bank forever. Roots and a priority order cover the variants that
# actually ship, and an unknown bank has a fair chance of working untouched.
#
# Order within each tuple is preference: a statement carrying both a transaction
# date and a value date should be read on the transaction date, because that's
# when the money was spent.
_DATE_ROOTS = ("transaction date", "txn date", "tran date", "posting date",
               "value date", "date")
_NARRATION_ROOTS = ("transaction remarks", "narration", "description",
                    "particulars", "remarks", "transaction details", "details")
_DEBIT_ROOTS = ("withdrawal", "debit", "dr")
_CREDIT_ROOTS = ("deposit", "credit", "cr")
_AMOUNT_ROOTS = ("transaction amount", "amount", "amt")
_TYPE_ROOTS = ("dr/cr", "drcr", "transaction type", "type")


def _normalise_header(cell: str) -> str:
    """Reduce a header to comparable words.

    Drops anything parenthesised — "(INR )", "(Rs.)" — then punctuation, then
    collapses whitespace. "Withdrawal Amount (INR )" and "Withdrawal Amt." both
    become "withdrawal amount"/"withdrawal amt", which share a root.
    """
    cell = re.sub(r"\([^)]*\)", " ", cell or "")
    cell = re.sub(r"[^a-z0-9/ ]+", " ", cell.lower())
    return re.sub(r"\s+", " ", cell).strip()


def _match_header(cells: list[str], roots: tuple[str, ...]) -> int | None:
    """Index of the column best matching these roots, or None.

    Roots are tried in order, so preference is expressed by the tuple rather
    than by whichever column happens to come first in the file.
    """
    normalised = [_normalise_header(c) for c in cells]
    for root in roots:
        for i, name in enumerate(normalised):
            if name == root or name.startswith(root + " ") or name.startswith(root):
                return i
    return None


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _to_amount(raw: str) -> float:
    """Indian-grouped money to a float. Blank, '-' and junk become 0."""
    cleaned = re.sub(r"[^\d.\-]", "", (raw or "").replace(",", ""))
    if cleaned in ("", "-", "."):
        return 0.0
    try:
        return abs(float(cleaned))
    except ValueError:
        return 0.0


def read_csv(data: bytes) -> list[Txn]:
    """Parse a bank CSV export.

    Banks put junk above the header — account number, address, a blank line or
    six — so the header row is *found* rather than assumed to be first.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")

    rows = list(csv.reader(io.StringIO(text)))
    header_at = cols = None
    for i, row in enumerate(rows[:40]):
        cells = [c.strip() for c in row]
        d = _match_header(cells, _DATE_ROOTS)
        n = _match_header(cells, _NARRATION_ROOTS)
        if d is not None and n is not None:
            header_at = i
            cols = {
                "date": d, "narration": n,
                "debit": _match_header(cells, _DEBIT_ROOTS),
                "credit": _match_header(cells, _CREDIT_ROOTS),
                "amount": _match_header(cells, _AMOUNT_ROOTS),
                "type": _match_header(cells, _TYPE_ROOTS),
            }
            break
    if header_at is None:
        raise StatementParseError(
            "Couldn't find a transaction table in that file. It needs a header row "
            "with a date column and a narration/description column."
        )

    out: list[Txn] = []
    for row in rows[header_at + 1:]:
        if not row or cols["date"] >= len(row):
            continue
        when = _parse_date(row[cols["date"]])
        if when is None:
            continue
        narration = row[cols["narration"]] if cols["narration"] < len(row) else ""
        debit = credit = 0.0
        if cols["debit"] is not None and cols["debit"] < len(row):
            debit = _to_amount(row[cols["debit"]])
        if cols["credit"] is not None and cols["credit"] < len(row):
            credit = _to_amount(row[cols["credit"]])
        # Single-amount layouts carry the direction in a separate column.
        if not debit and not credit and cols["amount"] is not None \
                and cols["amount"] < len(row):
            amount = _to_amount(row[cols["amount"]])
            kind = row[cols["type"]].strip().upper() if (
                cols["type"] is not None and cols["type"] < len(row)) else ""
            if kind.startswith("C") or "CR" in kind:
                credit = amount
            else:
                debit = amount
        if debit or credit:
            out.append(Txn(when=when, narration=narration.strip(),
                           debit=debit, credit=credit))
    if not out:
        raise StatementParseError(
            "Found a table but no transactions in it. If this is a PDF renamed "
            "to .csv, upload the PDF instead."
        )
    return out


# A statement line in a PDF, flattened: a date, some narration, then amounts.
_PDF_LINE_RE = re.compile(
    r"^\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+\w{3}\s+\d{2,4})\s+(.*)$")
_PDF_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{2,3})*\.\d{2}")


def read_pdf(data: bytes, password: str | None = None) -> list[Txn]:
    """Best-effort read of a PDF statement.

    Every bank lays these out differently, so this reads the shape most of them
    share — a date, a narration, then a trailing run of amounts — and takes the
    last two as (amount, balance). Where a layout doesn't fit, it finds little
    rather than finding the wrong thing, and the caller says so.

    Direction is inferred from the running balance: a falling balance is a
    debit. That's more reliable than looking for a Dr/Cr marker, which some
    banks omit and others put in a column that doesn't survive flattening.
    """
    import pdfplumber
    import pikepdf

    buf = io.BytesIO()
    try:
        with pikepdf.open(io.BytesIO(data), password=password or "") as pdf:
            pdf.save(buf)
    except pikepdf.PasswordError:
        raise StatementParseError(
            "Wrong password for that PDF. Bank statements are usually locked "
            "with your PAN, date of birth, or account number — check the email "
            "the bank sent with it."
        ) from None
    except Exception as exc:
        raise StatementParseError(f"Couldn't open that PDF: {exc}") from exc

    buf.seek(0)
    with pdfplumber.open(buf) as doc:
        text = "\n".join(page.extract_text() or "" for page in doc.pages)

    out: list[Txn] = []
    prev_balance: float | None = None
    for line in text.splitlines():
        m = _PDF_LINE_RE.match(line)
        if not m:
            continue
        when = _parse_date(m.group(1))
        if when is None:
            continue
        rest = m.group(2)
        amounts = [_to_amount(a) for a in _PDF_AMOUNT_RE.findall(rest)]
        if len(amounts) < 2:
            continue
        amount, balance = amounts[-2], amounts[-1]
        narration = _PDF_AMOUNT_RE.sub("", rest).strip(" .|-")

        if prev_balance is None:
            # First row: no balance to compare against, so fall back to the
            # narration. A credit here is rare and a misread costs one row.
            debit, credit = (0.0, amount) if is_salary(narration) else (amount, 0.0)
        elif balance < prev_balance:
            debit, credit = amount, 0.0
        else:
            debit, credit = 0.0, amount
        prev_balance = balance
        out.append(Txn(when=when, narration=narration, debit=debit, credit=credit))

    if not out:
        raise StatementParseError(
            "Couldn't find any transactions in that PDF. Most banks also offer a "
            "CSV or Excel download of the same statement — that reads far more "
            "reliably, and is worth using if you have the option."
        )
    return out


# --- Turning transactions into an expense picture ---------------------------

# Categories where a single large debit in one month is usually a *yearly* bill,
# not a monthly one. Annualising a school term fee or an insurance premium by
# twelve is the fastest way to produce a burn figure that is comically wrong —
# ₹1.2 lakh of fees becomes ₹14 lakh a year — so these default to annual when
# they appear once, and the review screen says the guess is a guess.
_LUMPY_CATEGORIES = {"healthcare", "education", "travel", "gifting"}


def suggest_frequency(slug: str, count: int) -> str:
    """Best guess at whether a category's spend recurs monthly or yearly.

    Anything seen more than once inside a single statement is recurring within
    that period, so it's monthly. A lone debit in a category that bills yearly is
    taken as annual. One month of data genuinely cannot distinguish these, which
    is why this is a default the user is asked to confirm rather than a finding.
    """
    if count >= 2:
        return "monthly"
    return "annual" if slug in _LUMPY_CATEGORIES else "monthly"


@dataclass
class CategorySum:
    slug: str
    total: float = 0.0
    count: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def frequency(self) -> str:
        return suggest_frequency(self.slug, self.count)

    def annual(self, months: float) -> float:
        """What this category costs in a year, on the suggested frequency.

        A monthly category scales by the period covered; an annual one is taken
        at face value, because the statement caught the whole year's bill.
        """
        if self.frequency == "annual":
            return self.total
        return self.total * (12.0 / months) if months else self.total * 12.0


@dataclass
class Analysis:
    """What a statement says about a year of spending."""

    start: date
    end: date
    days: int
    months: float
    by_category: list[CategorySum]
    spend_total: float              # in the period covered
    annual_total: float             # extrapolated
    excluded: dict[str, float]      # reason -> amount left out
    excluded_total: float
    income_total: float             # salary credits in the period
    annual_income: float
    txn_count: int
    unmatched_share: float          # fraction landing in "other"


# A month is not a year, and a statement covering three weeks is not a month.
# Annualising from too short a window produces a confident wrong number, so the
# caller is told how thin the evidence is rather than left to assume.
MIN_DAYS_FOR_CONFIDENCE = 25


def analyse(txns: list[Txn]) -> Analysis:
    """Sort, exclude, categorise and annualise. Pure — no storage, no side effects."""
    if not txns:
        raise StatementParseError("No transactions to analyse.")

    start = min(t.when for t in txns)
    end = max(t.when for t in txns)
    # Inclusive of both ends: a statement dated the 1st to the 31st covers 31 days.
    days = max(1, (end - start).days + 1)
    months = days / 30.44

    buckets: dict[str, CategorySum] = {}
    excluded: dict[str, float] = defaultdict(float)
    income = 0.0

    for t in txns:
        if t.credit and is_salary(t.narration):
            income += t.credit
        if not t.debit:
            continue
        category, reason = classify_txn(t.narration)
        if reason:
            excluded[reason] += t.debit
            continue
        bucket = buckets.setdefault(category, CategorySum(slug=category))
        bucket.total += t.debit
        bucket.count += 1
        if len(bucket.examples) < 3 and t.narration:
            bucket.examples.append(t.narration[:60])

    rows = sorted(buckets.values(), key=lambda b: -b.total)
    spend = sum(b.total for b in rows)
    other = next((b.total for b in rows if b.slug == DEFAULT_CATEGORY), 0.0)

    return Analysis(
        start=start, end=end, days=days, months=months,
        by_category=rows,
        spend_total=spend,
        # Per category, not one blanket multiplier: scaling a yearly school fee
        # by twelve is how this feature would lose people's trust on first use.
        annual_total=sum(b.annual(months) for b in rows),
        excluded=dict(excluded),
        excluded_total=sum(excluded.values()),
        income_total=income,
        annual_income=income * (365.0 / days),
        txn_count=len(txns),
        unmatched_share=(other / spend) if spend else 0.0,
    )


def merge(analyses: list[Analysis]) -> Analysis:
    """Combine several statements — multiple banks, or several months.

    Periods are unioned rather than summed: two banks covering the same month
    describe the same month, so annualising off the combined day count would
    halve the answer.
    """
    if not analyses:
        raise StatementParseError("Nothing to merge.")
    if len(analyses) == 1:
        return analyses[0]

    start = min(a.start for a in analyses)
    end = max(a.end for a in analyses)
    days = max(1, (end - start).days + 1)

    buckets: dict[str, CategorySum] = {}
    for a in analyses:
        for b in a.by_category:
            into = buckets.setdefault(b.slug, CategorySum(slug=b.slug))
            into.total += b.total
            into.count += b.count
            into.examples = (into.examples + b.examples)[:3]
    excluded: dict[str, float] = defaultdict(float)
    for a in analyses:
        for reason, amount in a.excluded.items():
            excluded[reason] += amount

    rows = sorted(buckets.values(), key=lambda b: -b.total)
    spend = sum(b.total for b in rows)
    other = next((b.total for b in rows if b.slug == DEFAULT_CATEGORY), 0.0)
    income = sum(a.income_total for a in analyses)
    months = days / 30.44

    return Analysis(
        start=start, end=end, days=days, months=months,
        by_category=rows,
        spend_total=spend,
        annual_total=sum(b.annual(months) for b in rows),
        excluded=dict(excluded),
        excluded_total=sum(excluded.values()),
        income_total=income,
        annual_income=income * (365.0 / days),
        txn_count=sum(a.txn_count for a in analyses),
        unmatched_share=(other / spend) if spend else 0.0,
    )
