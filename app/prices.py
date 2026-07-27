"""Live equity quotes from Yahoo Finance — the one deliberate network path.

The privacy invariant elsewhere in the app is "financial data never leaves the
machine". This module is the sanctioned exception, kept narrow on purpose:

  * Equities — the ONLY thing sent out is an exchange **ticker symbol** (e.g.
    "E2E") — never your units, values, holding sizes, PAN, or identity.
  * Mutual funds — we download AMFI's public **bulk** NAV file and look ISINs up
    *locally*, so nothing about your holdings leaves the machine at all.
  * Results are cached in-process (equity quotes ~15 min, the AMFI map ~6 h since
    NAV publishes once a day), so repeated page loads don't re-hit the network.
  * Every failure is swallowed (returns None / empty). A slow, blocked, or changed
    endpoint can never break a page render — the view falls back to statement values.

Yahoo's chart endpoint is unauthenticated and returns INR prices for NSE/BSE
symbols. AMFI's NAVAll.txt is a ';'-delimited public feed. Both are undocumented
shapes, hence the defensive parsing.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

_CACHE_TTL = 15 * 60  # seconds
_TIMEOUT = 6.0
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

# symbol -> (price, fetched_at). Only *successful* fetches are cached, so a
# transient failure retries on the next request rather than sticking for 15 min.
_cache: dict[str, tuple[float, float]] = {}
_lock = threading.Lock()


def yahoo_symbol(ticker: str | None) -> str | None:
    """Map a CAS exchange ticker to a Yahoo symbol.

    "E2E.NSE" -> "E2E.NS", "RELIANCE.BSE" -> "RELIANCE.BO". Returns None for an
    empty or unrecognised-exchange ticker rather than guessing.
    """
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t.endswith(".NSE"):
        return t[:-4] + ".NS"
    if t.endswith(".BSE"):
        return t[:-4] + ".BO"
    return None


def get_quote(symbol: str | None) -> float | None:
    """Latest price for a Yahoo symbol, cached ~15 min. None on any failure."""
    if not symbol:
        return None
    now = time.time()
    with _lock:
        hit = _cache.get(symbol)
        if hit is not None and now - hit[1] < _CACHE_TTL:
            return hit[0]
    price = _fetch(symbol)
    if price is not None:
        with _lock:
            _cache[symbol] = (price, now)
    return price


def _fetch(symbol: str) -> float | None:
    try:
        r = httpx.get(
            _URL.format(sym=symbol),
            params={"range": "1d", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price is not None else None
    except Exception:  # noqa: BLE001 — never let a quote failure surface into a request
        return None


def quotes_for_tickers(tickers: list[str | None]) -> dict[str, float]:
    """Live price per CAS ticker, for those that map to a symbol and fetch OK.

    Keyed by the original CAS ticker (e.g. "E2E.NSE") so callers can look up by the
    value stored on a holding. Fetches concurrently; missing/failed symbols are
    simply absent from the result.
    """
    symbols = {t: yahoo_symbol(t) for t in {tk for tk in tickers if tk}}
    wanted = {t: s for t, s in symbols.items() if s}
    if not wanted:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
        priced = pool.map(lambda item: (item[0], get_quote(item[1])), wanted.items())
    return {ticker: price for ticker, price in priced if price is not None}


# --- Mutual fund NAVs (AMFI) -------------------------------------------------

_AMFI_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
_AMFI_TTL = 6 * 60 * 60  # seconds — NAV publishes once daily

# (isin -> nav, fetched_at). The whole map is cached together; lookups are local.
_amfi_cache: tuple[dict[str, float], float] | None = None
_amfi_lock = threading.Lock()


def navs_for_isins(isins: list[str | None]) -> dict[str, float]:
    """Latest NAV per requested ISIN, from AMFI's public bulk feed (cached ~6 h).

    ISINs absent from the feed (non-MF holdings like SGBs, or unknown) are simply
    missing from the result. Nothing about the user is sent — the lookup is local.
    """
    wanted = {i for i in isins if i}
    if not wanted:
        return {}
    table = _amfi_navs()
    return {isin: table[isin] for isin in wanted if isin in table}


def _amfi_navs() -> dict[str, float]:
    """The full ISIN -> NAV map, fetched once and cached. Empty dict on failure."""
    global _amfi_cache
    now = time.time()
    with _amfi_lock:
        if _amfi_cache is not None and now - _amfi_cache[1] < _AMFI_TTL:
            return _amfi_cache[0]
    table = _fetch_amfi()
    if table:
        with _amfi_lock:
            _amfi_cache = (table, now)
        return table
    # On failure, serve a stale map if we have one rather than nothing.
    return _amfi_cache[0] if _amfi_cache is not None else {}


def _fetch_amfi() -> dict[str, float]:
    try:
        r = httpx.get(
            _AMFI_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30.0,
            follow_redirects=True,
        )
        r.raise_for_status()
        return _parse_amfi(r.text)
    except Exception:  # noqa: BLE001 — a NAV feed failure must not surface into a request
        return {}


def _parse_amfi(text: str) -> dict[str, float]:
    """Parse NAVAll.txt into {isin: nav}.

    Layout: a ';'-delimited row is
        Scheme Code; ISIN(Growth); ISIN(Reinvest); Scheme Name; NAV; Date
    interspersed with AMC-name / scheme-category headers (no ';') and blank lines.
    Both ISIN columns map to the same NAV; "-"/"N.A." values are skipped.
    """
    table: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue
        try:
            nav = float(parts[4].strip())
        except ValueError:
            continue  # "N.A.", "-", blank
        for isin in (parts[1].strip(), parts[2].strip()):
            if isin and isin != "-":
                table[isin] = nav
    return table
