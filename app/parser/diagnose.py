"""Describe *why* a statement wouldn't parse, without describing what's in it.

The point of this module is the feedback loop, not the user's immediate problem.
A parser can only be hardened against layouts you can see, and the app can never
see the statement itself — so it looks at the **shape** instead: which anchor
patterns matched, how many rows carry an ISIN, and what a data line looks like
with every letter replaced by X and every digit by 9.

That skeleton is enough to fix a regex and contains no name, no amount, no ISIN,
no folio and no PAN. It describes NSDL's typography, not anyone's money — which
is why it can be offered as something to send, and shown in full first so the
claim is checkable rather than trusted.

Nothing here raises: a diagnostic that fails on a broken file is worthless
precisely when it's needed.
"""

from __future__ import annotations

import re

from ._common import extract_text

# One line is enough to see the column layout, and fewer lines means less chance
# of an unexpected format leaking something the masking didn't anticipate.
_SAMPLE_LINES = 3
_ISIN_RE = re.compile(r"\bIN[EFD][0-9A-Z]{9}\b")
# Headings we expect to find. Which ones are missing localises the break.
_SECTIONS = ("Demat Account", "Mutual Fund Folios", "Consolidated Account Statement",
             "Equities", "NPS", "Holding Statement", "Portfolio Value")


def mask(line: str) -> str:
    """Letters to X, digits to 9, everything else kept.

    Punctuation and spacing survive because they *are* the diagnostic — the
    difference between "9,99,999.99" and "999999.99" is exactly the kind of
    thing that breaks an amount regex. Nothing readable survives.
    """
    return re.sub(r"[A-Za-z]", "X", re.sub(r"[0-9]", "9", line))


def diagnose(file_bytes: bytes, password: str | None, kind: str = "NSDL") -> dict:
    """A structural report on a statement that wouldn't parse. Never raises."""
    report: dict = {"kind": kind, "opened": False, "chars": 0, "lines": 0,
                    "isin_lines": 0, "sections_found": [], "sections_missing": [],
                    "anchors": {}, "samples": [], "error": None}
    try:
        text = extract_text(file_bytes, password)
    except Exception as exc:                      # noqa: BLE001 — this is the report
        report["error"] = type(exc).__name__
        return report

    report["opened"] = True
    report["chars"] = len(text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    report["lines"] = len(lines)

    isin_lines = [ln for ln in lines if _ISIN_RE.search(ln)]
    report["isin_lines"] = len(isin_lines)
    report["samples"] = [mask(ln)[:120] for ln in isin_lines[:_SAMPLE_LINES]]

    for name in _SECTIONS:
        (report["sections_found"] if name.lower() in text.lower()
         else report["sections_missing"]).append(name)

    # Which anchors the parser needs, and which of them this file satisfies.
    from . import nsdl_cas as n
    report["anchors"] = {
        "statement date": any(p.search(text) for p in n._DATE_PATTERNS),
        "portfolio total": any(p.search(text) for p in n._TOTAL_PATTERNS),
        "any ISIN row": bool(isin_lines),
    }
    return report


def as_text(report: dict) -> str:
    """The report as something a person can read and paste into a bug report."""
    out = [f"{report['kind']} statement diagnostic (no financial data)"]
    if report.get("error"):
        out.append(f"  could not read the file: {report['error']}")
        return "\n".join(out)
    out += [
        f"  text extracted : yes, {report['chars']:,} chars over {report['lines']:,} lines",
        f"  ISIN-bearing rows : {report['isin_lines']}",
    ]
    for name, hit in report["anchors"].items():
        out.append(f"  {name:<17}: {'matched' if hit else 'NO MATCH'}")
    if report["sections_missing"]:
        out.append(f"  headings not found: {', '.join(report['sections_missing'])}")
    if report["samples"]:
        out.append("  row shapes (letters masked to X, digits to 9):")
        out += [f"    {s}" for s in report["samples"]]
    return "\n".join(out)
