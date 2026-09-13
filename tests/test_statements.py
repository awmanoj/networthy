"""Reading a bank statement into an expense picture.

The risk here isn't parsing — it's arithmetic that looks fine and is wrong. Two
failure modes dominate, and both inflate the answer: counting debits that aren't
spending (a card bill, an EMI, a SIP), and annualising a yearly bill by twelve.
Most of these tests are about those.
"""

from datetime import date

import pytest

from app import statements as st


def _csv(*rows: str) -> bytes:
    head = ("Some Bank Ltd\nAccount 1234567890\n\n"
            "Date,Narration,Withdrawal Amt,Deposit Amt,Closing Balance\n")
    return (head + "\n".join(rows) + "\n").encode()


# --- Reading ----------------------------------------------------------------

def test_csv_header_is_found_below_the_bank_preamble():
    """Banks put the account number and address above the table."""
    txns = st.read_csv(_csv("01/08/2026,UPI/swiggy/dinner,1250.00,,10000.00"))
    assert len(txns) == 1
    assert txns[0].when == date(2026, 8, 1)
    assert txns[0].debit == 1250.0


def test_indian_grouped_amounts_and_blank_columns():
    txns = st.read_csv(_csv("01/08/2026,RENT,\"1,25,000.00\",,10000.00",
                            "02/08/2026,SALARY,,\"4,25,000.00\",435000.00"))
    assert txns[0].debit == 125000.0 and txns[0].credit == 0.0
    assert txns[1].credit == 425000.0 and txns[1].debit == 0.0


def test_single_amount_column_uses_the_type_column():
    data = (b"Date,Description,Amount,Type\n"
            b"01/08/2026,UPI/dmart,5000.00,DR\n"
            b"02/08/2026,SALARY CREDIT,90000.00,CR\n")
    txns = st.read_csv(data)
    assert txns[0].debit == 5000.0
    assert txns[1].credit == 90000.0


def test_a_file_with_no_transaction_table_is_rejected_clearly():
    with pytest.raises(st.StatementParseError, match="header row"):
        st.read_csv(b"just,some,columns\n1,2,3\n")


# --- Exclusions: the numbers that must NOT be counted -----------------------

@pytest.mark.parametrize("narration,reason", [
    ("CREDIT CARD PAYMENT AUTOPAY HDFC", "credit-card bill"),
    ("CC BILL PAYMENT", "credit-card bill"),
    ("NACH SIP HDFC MUTUAL FUND", "investment"),
    ("ZERODHA BROKING LTD", "investment"),
    ("PPF DEPOSIT", "investment"),
    ("HOME LOAN EMI HDFC LTD", "loan EMI"),
    ("CAR LOAN INSTALMENT", "loan EMI"),
    ("TRANSFER TO OWN AC SAVINGS", "self-transfer"),
])
def test_debits_that_are_not_spending(narration, reason):
    category, got = st.classify_txn(narration)
    assert category is None and got == reason


def test_a_card_bill_would_otherwise_double_the_burn():
    """The card's purchases are the spending; the bill settles them. Counting
    both is the single biggest way to double a burn figure."""
    a = st.analyse(st.read_csv(_csv(
        "01/08/2026,UPI/swiggy/dinner,1000.00,,10000.00",
        "28/08/2026,CREDIT CARD PAYMENT HDFC,64200.00,,10000.00")))
    assert a.spend_total == 1000.0
    assert a.excluded["credit-card bill"] == 64200.0


def test_emis_stay_out_because_they_live_under_liabilities():
    a = st.analyse(st.read_csv(_csv(
        "05/08/2026,HOME LOAN EMI,78000.00,,10000.00",
        "06/08/2026,BESCOM ELECTRICITY,4200.00,,10000.00")))
    assert "loan EMI" in a.excluded
    assert [b.slug for b in a.by_category] == ["utilities"]


# --- Categorisation ---------------------------------------------------------

@pytest.mark.parametrize("narration,slug", [
    ("UPI/bigbasket/order", "food"),
    ("UPI/swiggy/dinner", "dining"),
    ("UPI/uber/ride", "transport"),
    ("BESCOM ELECTRICITY BILL", "utilities"),
    ("RENT TO LANDLORD", "housing"),
    ("SCHOOL FEES TERM 2", "education"),
    ("STAR HEALTH POLICY PREMIUM", "healthcare"),
    ("UPI/maid salary", "domestic-help"),
    ("MAKEMYTRIP BOOKING", "travel"),
    ("UPI/amazon/order", "lifestyle"),
    ("UPI/somethingunknown/xyz", "other"),
])
def test_categories(narration, slug):
    assert st.classify_txn(narration)[0] == slug


def test_groceries_beat_generic_shopping():
    """Merchant rules are ordered so a grocery delivery is food, not lifestyle."""
    assert st.classify_txn("UPI/bigbasket via amazon pay")[0] == "food"


# --- Annualising: the arithmetic that looks fine and is wrong ---------------

def test_a_yearly_bill_is_not_multiplied_by_twelve():
    """₹1.2 lakh of school fees in one month is not ₹14 lakh a year. Getting
    this wrong is how the feature would lose trust on first use."""
    a = st.analyse(st.read_csv(_csv(
        "01/08/2026,SCHOOL FEES TERM 2,120000.00,,10000.00",
        "31/08/2026,BESCOM ELECTRICITY,4000.00,,10000.00")))
    education = next(b for b in a.by_category if b.slug == "education")
    assert education.frequency == "annual"
    assert education.annual(a.months) == 120000.0
    assert a.annual_total < 200000.0        # not ~1.5 million


def test_a_category_seen_twice_is_treated_as_monthly():
    a = st.analyse(st.read_csv(_csv(
        "01/08/2026,APOLLO PHARMACY,2000.00,,10000.00",
        "20/08/2026,MEDPLUS CHEMIST,1500.00,,10000.00")))
    health = next(b for b in a.by_category if b.slug == "healthcare")
    assert health.frequency == "monthly"
    assert health.annual(a.months) == pytest.approx(3500 * 12 / a.months, rel=1e-6)


def test_the_period_comes_from_the_dates_not_an_assumed_month():
    a = st.analyse(st.read_csv(_csv(
        "01/08/2026,UPI/dmart,1000.00,,10000.00",
        "15/08/2026,UPI/dmart,1000.00,,10000.00")))
    assert a.days == 15                      # inclusive of both ends
    assert a.annual_total > 2000 * 12        # a fortnight scales up, not by 12


def test_a_short_window_is_flagged_rather_than_trusted():
    a = st.analyse(st.read_csv(_csv("01/08/2026,UPI/dmart,1000.00,,10000.00")))
    assert a.days < st.MIN_DAYS_FOR_CONFIDENCE


# --- Income -----------------------------------------------------------------

def test_salary_credits_become_income():
    a = st.analyse(st.read_csv(_csv(
        "01/08/2026,NEFT CR ACME CORP SALARY AUG 2026,,425000.00,500000.00",
        "02/08/2026,UPI/dmart,5000.00,,495000.00")))
    assert a.income_total == 425000.0
    assert a.annual_income == pytest.approx(425000 * 365 / a.days)


def test_a_plain_credit_is_not_called_income():
    """A refund or an incoming transfer isn't salary, and guessing wrong here is
    worse than reporting nothing."""
    a = st.analyse(st.read_csv(_csv(
        "01/08/2026,UPI REFUND AMAZON,,2500.00,10000.00",
        "02/08/2026,NEFT CR FROM FRIEND,,50000.00,60000.00")))
    assert a.income_total == 0.0


# --- Merging several statements ---------------------------------------------

def test_two_banks_over_the_same_month_do_not_halve_the_answer():
    """Periods are unioned, not summed: two statements covering August describe
    one August. Adding their day counts would double the window and halve the
    annual figure."""
    one = st.analyse(st.read_csv(_csv("01/08/2026,UPI/dmart,10000.00,,1.00",
                                      "31/08/2026,UPI/dmart,10000.00,,1.00")))
    two = st.analyse(st.read_csv(_csv("01/08/2026,UPI/swiggy/a,5000.00,,1.00",
                                      "31/08/2026,UPI/swiggy/b,5000.00,,1.00")))
    merged = st.merge([one, two])
    assert merged.days == one.days == two.days == 31
    assert merged.spend_total == 30000.0
    assert merged.annual_total == pytest.approx(30000 * 12 / merged.months, rel=1e-6)


def test_merging_keeps_every_category_and_exclusion():
    one = st.analyse(st.read_csv(_csv("01/08/2026,RENT,50000.00,,1.00")))
    two = st.analyse(st.read_csv(_csv("05/09/2026,HOME LOAN EMI,78000.00,,1.00",
                                      "06/09/2026,UPI/uber,500.00,,1.00")))
    merged = st.merge([one, two])
    assert {b.slug for b in merged.by_category} == {"housing", "transport"}
    assert merged.excluded["loan EMI"] == 78000.0
    assert merged.start == date(2026, 8, 1) and merged.end == date(2026, 9, 6)


# --- The route: nothing is written until the user confirms ------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import prices, storage
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "t.db")
    storage.init_db()
    monkeypatch.setattr(prices, "quotes_for_tickers", lambda t: {})
    monkeypatch.setattr(prices, "navs_for_isins", lambda i: {})
    monkeypatch.setattr(prices, "get_quote", lambda s: None)
    from fastapi.testclient import TestClient
    import app.main as m
    return TestClient(m.app)


def _login(email="imp@test.com"):
    from datetime import datetime, timedelta
    from app import auth, storage
    uid = storage.get_or_create_user(email).id
    storage.create_session(uid, "tok", datetime.utcnow() + timedelta(hours=1))
    return uid, {auth.SESSION_COOKIE: "tok"}


_SAMPLE = _csv(
    "01/08/2026,NEFT CR ACME SALARY AUG,,425000.00,500000.00",
    "02/08/2026,UPI/bigbasket/order,8450.00,,491550.00",
    "03/08/2026,NACH SIP HDFC MUTUAL FUND,50000.00,,441550.00",
    "09/08/2026,RENT TO LANDLORD,65000.00,,376550.00",
    "28/08/2026,CREDIT CARD PAYMENT HDFC,64200.00,,312350.00",
)


def test_upload_reviews_without_saving_anything(client):
    from app import storage
    uid, ck = _login()
    r = client.post("/expenses/import", cookies=ck,
                    files={"files": ("aug.csv", _SAMPLE, "text/csv")})
    assert r.status_code == 200
    assert "Nothing has been saved yet" in r.text
    assert storage.list_expenses(uid) == [], "the import wrote rows before review"
    # The exclusions are shown, not silently applied.
    assert "credit-card bill" in r.text and "investment" in r.text


def test_only_ticked_rows_are_written_on_confirm(client):
    from app import storage
    uid, ck = _login("confirm@test.com")
    r = client.post("/expenses/import/confirm", cookies=ck, follow_redirects=False, data={
        "include_housing": "on", "amount_housing": "65000",
        "frequency_housing": "monthly", "label_housing": "Rent",
        # food left unticked
        "amount_food": "8450", "frequency_food": "monthly", "label_food": "Groceries",
        "annual_income": "5100000",
    })
    assert r.status_code == 303
    rows = storage.list_expenses(uid)
    assert [x["name"] for x in rows] == ["Rent"]
    assert rows[0]["category"] == "housing" and rows[0]["amount"] == 65000.0
    assert storage.get_annual_income(uid) == 5100000.0


def test_a_bogus_category_or_frequency_is_ignored(client):
    from app import storage
    uid, ck = _login("bogus@test.com")
    client.post("/expenses/import/confirm", cookies=ck, follow_redirects=False, data={
        "include_notacategory": "on", "amount_notacategory": "999",
        "include_housing": "on", "amount_housing": "1000",
        "frequency_housing": "../../etc", "label_housing": "Rent",
    })
    rows = storage.list_expenses(uid)
    assert len(rows) == 1
    assert rows[0]["frequency"] == "monthly"     # fell back, didn't store junk


def test_an_unreadable_file_returns_to_expenses_with_the_reason(client):
    _uid, ck = _login("bad@test.com")
    r = client.post("/expenses/import", cookies=ck,
                    files={"files": ("notes.csv", b"hello world", "text/csv")})
    assert r.status_code == 200
    assert "header row" in r.text or "Couldn't" in r.text


def test_import_requires_a_session(client):
    r = client.post("/expenses/import", follow_redirects=False,
                    files={"files": ("a.csv", _SAMPLE, "text/csv")})
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_income_is_in_the_export_and_deleted_with_everything_else(client):
    from app import exporter, storage
    uid, ck = _login("exp@test.com")
    storage.save_annual_income(uid, 4_200_000.0)
    assert exporter.collect(uid)["user_settings"][0]["annual_income"] == 4_200_000.0
    exporter.delete_everything(uid)
    assert storage.get_annual_income(uid) is None


# --- Generalising across banks ----------------------------------------------
#
# Every bank names and decorates these columns differently. Matching whole
# header names means a rule per bank forever, so matching is by word root —
# "Withdrawal Amount (INR )", "Withdrawals" and "Withdrawal Amt." all reduce to
# the same thing. These are realistic header rows from the banks people use.

BANK_HEADERS = {
    "HDFC": b"Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
            b"01/08/26,UPI-SWIGGY-PAY,REF1,01/08/26,1250.00,,50000.00\n",
    "ICICI": b"S No.,Value Date,Transaction Date,Cheque Number,Transaction Remarks,"
             b"Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )\n"
             b"1,01/08/2026,01/08/2026,,UPI/DMART/xyz,5000.00,,45000.00\n",
    "SBI": b"Txn Date,Value Date,Description,Ref No./Cheque No.,Debit,Credit,Balance\n"
           b"1 Aug 2026,1 Aug 2026,BY TRANSFER-UPI/SWIGGY,REF,1250.00,,50000.00\n",
    "Axis": b"Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL\n"
            b"01-08-2026,,UPI/P2M/BIGBASKET,8450.00,,41550.00,123\n",
    "Kotak": b"Sl. No.,Transaction Date,Value Date,Description,Chq / Ref number,Debit,Credit,Balance\n"
             b"1,01-08-2026,01-08-2026,UPI/UBER INDIA,REF,430.00,,41120.00\n",
    "IDFC": b"Transaction Date,Value Date,Particulars,Cheque No,Debit,Credit,Balance\n"
            b"01/08/2026,01/08/2026,UPI-AMAZON,,3400.00,,37720.00\n",
    "YesBank": b"Transaction Date,Value Date,Description,Withdrawals,Deposits,Balance\n"
               b"01/08/2026,01/08/2026,NEFT RENT,65000.00,,37720.00\n",
    "IndusInd": b"Date,Value Date,Description,Debit Amount,Credit Amount,Balance Amount\n"
                b"01/08/2026,01/08/2026,POS PURCHASE DMART,2500.00,,35220.00\n",
    "BoB": b"Sr.No.,Tran Date,Remarks,Withdrawal,Deposit,Balance\n"
           b"1,01-08-2026,ATM CASH,2000.00,,33220.00\n",
}


@pytest.mark.parametrize("bank", sorted(BANK_HEADERS))
def test_reads_each_banks_column_naming(bank):
    txns = st.read_csv(BANK_HEADERS[bank])
    assert len(txns) == 1, f"{bank}: no transaction read"
    assert txns[0].debit > 0, f"{bank}: debit column not found"
    assert txns[0].when == date(2026, 8, 1), f"{bank}: date not parsed"


def test_parenthesised_currency_suffixes_are_ignored():
    """ICICI writes "Withdrawal Amount (INR )" — the decoration must not stop
    the column being recognised."""
    assert st._normalise_header("Withdrawal Amount (INR )") == "withdrawal amount"
    assert st._normalise_header("Withdrawal Amt.") == "withdrawal amt"
    assert st._normalise_header("Balance (Rs.)") == "balance"


def test_the_transaction_date_wins_over_the_value_date():
    """Several banks carry both. Spending happened on the transaction date, and
    for a statement spanning a month-end the two can fall either side of it."""
    data = (b"Value Date,Transaction Date,Description,Debit,Credit,Balance\n"
            b"31/07/2026,01/08/2026,UPI/DMART,5000.00,,45000.00\n")
    assert st.read_csv(data)[0].when == date(2026, 8, 1)


def test_an_unknown_bank_with_ordinary_names_still_works():
    """The point of roots: a bank nobody has tested has a fair chance untouched."""
    data = (b"Posting Date,Transaction Details,Debit Amt,Credit Amt,Running Balance\n"
            b"01/08/2026,PURCHASE AT STORE,1500.00,,20000.00\n")
    txns = st.read_csv(data)
    assert txns[0].debit == 1500.0 and txns[0].when == date(2026, 8, 1)
