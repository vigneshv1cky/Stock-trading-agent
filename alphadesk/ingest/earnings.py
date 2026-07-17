"""Earnings calendar — who reported (with the EPS surprise) and who's about to.

Pulls per-ticker earnings dates from yfinance over a liquid watchlist and stores
them in the ledger. Two consumers:
  • upcoming()          → "be ready": what reports in the next N days
  • recently_reported() → post-earnings-drift candidates (reported, surprise known)

No new API key — yfinance gives estimate / actual / surprise% per ticker. The
watchlist is a plain JSON file the user can grow (~/.alphadesk/earnings_watchlist.json),
seeded with liquid large caps on first run.
"""

import json
import logging

from alphadesk.config import DATA_DIR
from alphadesk.ledger import store

log = logging.getLogger("alphadesk.earnings")

_WATCHLIST_FILE = DATA_DIR / "earnings_watchlist.json"

# Seed: liquid large caps whose earnings actually move and are tradeable. Grow the
# JSON file to widen coverage; per-ticker yfinance calls are the cost, so keep it
# to names you'd actually trade.
_DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "ORCL", "AMD",
    "NFLX", "CRM", "ADBE", "INTC", "QCOM", "CSCO", "TXN", "MU", "AMAT", "PANW",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "SCHW",
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR", "BMY",
    "XOM", "CVX", "COP", "SLB",
    "WMT", "COST", "HD", "LOW", "TGT", "PG", "KO", "PEP", "MCD", "SBUX", "NKE", "DIS",
    "CAT", "BA", "GE", "HON", "UPS", "RTX", "LMT", "DE",
    "T", "VZ", "TMUS", "NEE", "UNP",
]


def _load_watchlist() -> list[str]:
    if _WATCHLIST_FILE.exists():
        try:
            return json.loads(_WATCHLIST_FILE.read_text())
        except Exception:
            pass
    _WATCHLIST_FILE.write_text(json.dumps(sorted(_DEFAULT_WATCHLIST), indent=0))
    return list(_DEFAULT_WATCHLIST)


def _session(dt) -> str:
    h, m = dt.hour, dt.minute
    if h >= 16:
        return "AMC"           # after market close
    if h < 9 or (h == 9 and m < 30):
        return "BMO"           # before market open
    return "DAY"


def _f(v):
    try:
        f = float(v)
        return f if f == f else None   # drop NaN
    except (TypeError, ValueError):
        return None


def refresh_calendar(symbols: list[str] | None = None, limit: int = 8) -> int:
    """Pull each ticker's earnings dates (past + upcoming) into the ledger.
    Returns the number of rows upserted."""
    import yfinance as yf

    watch = symbols or _load_watchlist()
    rows: list[dict] = []
    for sym in watch:
        try:
            df = yf.Ticker(sym).get_earnings_dates(limit=limit)
        except Exception as exc:
            log.warning("earnings fetch failed for %s: %s", sym, exc)
            continue
        if df is None or df.empty:
            continue
        for dt, r in df.iterrows():
            rows.append({
                "symbol": sym,
                "report_date": dt.isoformat(),
                "session": _session(dt),
                "eps_estimate": _f(r.get("EPS Estimate")),
                "eps_actual": _f(r.get("Reported EPS")),
                "surprise_pct": _f(r.get("Surprise(%)")),
            })
    store.upsert_earnings(rows)
    log.info("earnings calendar refreshed: %d rows across %d tickers", len(rows), len(watch))
    return len(rows)
