# Networthy

Track your net worth across the years by parsing your **NSDL CAS** (Consolidated
Account Statement) — the single PDF that consolidates your demat holdings (NSDL +
CDSL) and mutual fund folios.

Upload a CAS PDF, Networthy extracts the total valuation as a dated snapshot, and
the dashboard charts how your net worth has moved over time.

> **Privacy:** everything runs locally. Statements and the parsed database live
> under `data/` and are gitignored. Nothing leaves your machine.

## How it works

1. Every month/quarter, download your CAS from https://nsdl.co.in (CAS → NSDL) or
   https://www.camsonline.com. It arrives as a **password-protected PDF**
   (password is usually your PAN in the format the email specifies).
2. Upload the PDF + password in Networthy.
3. It parses the total portfolio value and stores a snapshot keyed by the
   statement date.
4. The dashboard plots net worth over time.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

## Project layout

```
app/
  main.py            FastAPI app + routes
  models.py          Dataclasses for parsed statements & snapshots
  storage.py         SQLite persistence (data/networthy.db)
  parser/
    nsdl_cas.py      NSDL CAS PDF parsing
  templates/         Jinja2 templates
  static/            CSS
data/                SQLite DB + working files (gitignored)
tests/               Parser tests
```

## Status

Early scaffold. The CAS layout parser (`app/parser/nsdl_cas.py`) is the core
piece and needs to be hardened against real statement variations — see the TODOs
in that file.
