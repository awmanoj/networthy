"""Live equity quotes from Yahoo Finance — the one deliberate network path.

The privacy invariant elsewhere in the app is "financial data never leaves the
machine". This module is the sanctioned exception, kept narrow on purpose:

  * The ONLY thing sent out is an exchange **ticker symbol** (e.g. "E2E") — never
    your units, values, holding sizes, PAN, or identity.
  * Quotes are cached in-process for ~15 minutes, so repeated page loads don't
    re-hit the API.
  * Every failure is swallowed (returns None). A slow, blocked, or changed API can
    never break a page render — the view just falls back to statement-date values.

Yahoo's chart endpoint is unauthenticated and returns INR prices for NSE/BSE
symbols. It's an undocumented endpoint, hence the defensive parsing.
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
