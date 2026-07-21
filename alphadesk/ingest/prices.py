"""Price CONTEXT service — lazy, per-symbol, TTL-cached. NO triggers, NO sweeps.

Price never decides what gets analyzed (that's information's job); it only
answers factual questions for symbols already under attention:
  • what's the recent price action? (briefs, scout fields)
  • has a neighbor already moved? (ripple priced-check)
  • how liquid is it? (LOW_LIQUIDITY evidence tag, friction scaling)

Plus one movers() call per scout window — a fact ranking, not a filter.
"""

import logging
import threading
import time
from typing import Any, Optional

from alphadesk.config import LOW_LIQUIDITY_DOLLAR_VOL, now_et

log = logging.getLogger("alphadesk.prices")

_TTL_S = 120
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def get_context(symbol: str) -> Optional[dict]:
    """Price/liquidity context for one symbol (fetched on demand, cached)."""
    sym = symbol.upper()
    with _cache_lock:
        hit = _cache.get(sym)
        if hit and time.time() - hit[0] < _TTL_S:
            return hit[1]
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period="90d", interval="1d")
        if df is None or len(df) < 5:
            return None
        closes = df["Close"].astype(float)
        vols = df["Volume"].astype(float)
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2])
        avg_dollar_vol = float((closes * vols).tail(20).mean())
        # Relative volume: the last COMPLETED session's volume vs its own recent
        # norm — a confirmation/participation fact (is the news being acted on, or
        # ignored?). We skip an in-progress bar: intraday, yfinance's latest daily
        # bar is partial, so partial ÷ full-day norm reads misleadingly low for
        # every name. Reference the prior completed session instead; baseline is the
        # 20 sessions before it. Evidence the agents weigh, never a code threshold.
        n = len(vols)
        ref = n - 1
        if df.index[-1].date() == now_et().date() and n > 1:
            ref = n - 2   # current bar is live/partial — use the last closed session
        base_vols = vols.iloc[max(0, ref - 20):ref]
        base_vol = float(base_vols.mean()) if len(base_vols) else 0.0
        rvol = round(float(vols.iloc[ref]) / base_vol, 2) if base_vol else None
        ctx = {
            "symbol": sym,
            "last_price": round(last, 4),
            "change_today_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
            "change_5d_pct": round((last - float(closes.iloc[-6])) / float(closes.iloc[-6]) * 100, 2)
            if len(closes) > 6 else 0.0,
            "change_20d_pct": round((last - float(closes.iloc[-21])) / float(closes.iloc[-21]) * 100, 2)
            if len(closes) > 21 else 0.0,
            "high_90d": round(float(closes.max()), 2),
            "low_90d": round(float(closes.min()), 2),
            "avg_dollar_vol": round(avg_dollar_vol),
            "rvol": rvol,          # latest-session volume ÷ its 20-session norm
            "low_liquidity": avg_dollar_vol < LOW_LIQUIDITY_DOLLAR_VOL,
            "closes_10d": [round(float(c), 2) for c in closes.tail(10)],
        }
        with _cache_lock:
            _cache[sym] = (time.time(), ctx)
        return ctx
    except Exception as exc:
        log.debug("price context failed %s: %s", sym, exc)
        return None


_fund_cache: dict[str, tuple[float, dict | None]] = {}
_FUND_TTL_S = 3600


def get_fundamentals(symbol: str) -> Optional[dict]:
    """Basic valuation/quality facts (best-effort via yfinance; cached 1h)."""
    sym = symbol.upper()
    with _cache_lock:
        hit = _fund_cache.get(sym)
        if hit and time.time() - hit[0] < _FUND_TTL_S:
            return hit[1]
    out: dict | None = None
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
        out = {
            "market_cap": info.get("marketCap"),
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "profit_margin": info.get("profitMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        }
        if not any(v is not None for v in out.values()):
            out = None
    except Exception as exc:
        log.debug("fundamentals failed %s: %s", sym, exc)
    with _cache_lock:
        _fund_cache[sym] = (time.time(), out)
    return out


_opt_cache: dict[str, tuple[float, dict | None]] = {}
_OPT_TTL_S = 900   # 15m — IV/expected-move drift slowly enough intraday


def _mid(row) -> Optional[float]:
    """Bid/ask midpoint, falling back to last trade; None if neither is usable."""
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    last = float(row.get("lastPrice") or 0)
    return last if last > 0 else None


def get_options_context(symbol: str) -> Optional[dict]:
    """Options-implied expected move + ATM IV — the market's own 'how much is
    already priced in' number, the quantitative anchor for the priced-in debate.

    Best-effort via yfinance, cached 15m, fail-open (None when a name has no
    options or the chain is too illiquid to trust). Pure facts — the agents weigh
    them; nothing here decides anything (design law #2). Two independent reads:
      • expected_move_to_expiry_pct — the ATM straddle mid ÷ spot: the market's
        actual quoted move to the nearest expiry (ground truth, no term-structure
        assumption).
      • expected_move_{1,5,10}d_pct — ATM IV projected over standard trading-day
        windows (sqrt-time), so the desk can match it to the pick's horizon.
    """
    import math

    sym = symbol.upper()
    with _cache_lock:
        hit = _opt_cache.get(sym)
        if hit and time.time() - hit[0] < _OPT_TTL_S:
            return hit[1]

    out: dict | None = None
    try:
        import pandas as pd
        import yfinance as yf

        ctx = get_context(sym)
        spot = float(ctx["last_price"]) if ctx and ctx.get("last_price") else 0.0
        if not spot:
            raise ValueError("no spot price")

        tk = yf.Ticker(sym)
        expiries = tk.options or ()
        if not expiries:
            raise ValueError("no listed options")

        # nearest expiry ≥2 calendar days out (skip 0-1 DTE gamma noise)
        today = now_et().date()
        exp, dte = None, 0
        for e in expiries:
            d = (pd.Timestamp(e).date() - today).days
            if d >= 2:
                exp, dte = e, d
                break
        if exp is None:  # only ultra-short expiries listed — take the furthest
            exp = expiries[-1]
            dte = max(1, (pd.Timestamp(exp).date() - today).days)

        chain = tk.option_chain(exp)
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
            raise ValueError("empty chain")

        call = calls.iloc[(calls["strike"] - spot).abs().argmin()]
        put = puts.iloc[(puts["strike"] - spot).abs().argmin()]

        ivs = [float(v) for v in (call.get("impliedVolatility"), put.get("impliedVolatility"))
               if v and float(v) > 0]
        atm_iv = sum(ivs) / len(ivs) if ivs else None   # decimal, annualized

        cm, pm = _mid(call), _mid(put)
        straddle = cm + pm if (cm and pm) else None
        em_expiry = round(straddle / spot * 100, 2) if straddle else None
        if em_expiry and em_expiry > 100:  # nonsense from a broken/illiquid quote
            em_expiry = None

        if atm_iv is None and em_expiry is None:
            raise ValueError("no usable IV or straddle")

        def _em_days(nd: int) -> float | None:
            return round(atm_iv * math.sqrt(nd / 252) * 100, 2) if atm_iv else None

        out = {
            "atm_iv_pct": round(atm_iv * 100, 1) if atm_iv else None,
            "expiry": exp,
            "days_to_expiry": dte,
            "expected_move_to_expiry_pct": em_expiry,
            "expected_move_1d_pct": _em_days(1),
            "expected_move_5d_pct": _em_days(5),
            "expected_move_10d_pct": _em_days(10),
        }
    except Exception as exc:
        log.debug("options context failed %s: %s", sym, exc)
        out = None
    with _cache_lock:
        _opt_cache[sym] = (time.time(), out)
    return out


_earn_move_cache: dict[str, Any] = {"ts": 0.0, "key": None, "data": {}}


def moves_since_report(items: list[dict], ttl: int = 300) -> dict[str, Optional[float]]:
    """% price move since each name's earnings went public — the real drift, a hard
    price fact (no EPS-basis ambiguity). Baseline is the last close BEFORE the report
    was public (session-aware: AMC → report-day close; BMO/other → prior close);
    current is the latest close. One batched yfinance download for all names, cached.

    items: [{symbol, report_date, session}]. Returns {symbol: pct move | None}.
    """
    import pandas as pd

    key = repr(sorted((i["symbol"], i["report_date"], i.get("session")) for i in items))
    now = time.time()
    with _cache_lock:
        c = _earn_move_cache
        if c["key"] == key and now - c["ts"] < ttl:
            return c["data"]

    syms = sorted({i["symbol"] for i in items})
    out: dict[str, Optional[float]] = {s: None for s in syms}
    if syms:
        try:
            import yfinance as yf
            df = yf.download(syms, period="20d", interval="1d", group_by="ticker",
                             progress=False, threads=True, auto_adjust=True)
            for i in items:
                sym, rd, sess = i["symbol"], i["report_date"], i.get("session")
                try:
                    sub = df[sym] if len(syms) > 1 else df
                    closes = sub["Close"].dropna()
                    if closes.empty:
                        continue
                    days = closes.index.normalize()
                    rdts = pd.Timestamp(rd).normalize()
                    mask = (days <= rdts) if sess == "AMC" else (days < rdts)
                    base_days = closes.index[mask]
                    if len(base_days) == 0:
                        continue
                    base = float(closes.loc[base_days[-1]])
                    cur = float(closes.iloc[-1])
                    out[sym] = round((cur - base) / base * 100, 2) if base else None
                except Exception:
                    continue
        except Exception as exc:
            log.debug("earnings moves download failed: %s", exc)

    with _cache_lock:
        _earn_move_cache.update(ts=now, key=key, data=out)
    return out


def latest_prices(symbols: list[str]) -> dict[str, float]:
    """Real-time last-trade prices, batched in one Alpaca call (fallback: the
    cached yfinance context per missing symbol). For live position tracking."""
    out: dict[str, float] = {}
    syms = sorted({s.upper() for s in symbols if s})
    if not syms:
        return out
    try:
        import os
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        client = StockHistoricalDataClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
        trades = client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=syms))
        for sym, trade in trades.items():
            try:
                out[sym] = round(float(trade.price), 4)
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        log.debug("alpaca latest_prices failed: %s", exc)
    for sym in syms:                       # fill any gaps from the yfinance context
        if sym not in out:
            ctx = get_context(sym)
            if ctx and ctx.get("last_price") is not None:
                out[sym] = float(ctx["last_price"])
    return out


def movers(limit: int = 10) -> list[dict[str, Any]]:
    """Top movers FYI ranking from Alpaca's screener — a fact, not a filter."""
    try:
        import os
        from alpaca.data.requests import MarketMoversRequest
        from alpaca.data.screener import ScreenerClient
        client = ScreenerClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
        result = client.get_market_movers(MarketMoversRequest(top=limit))
        out = []
        for direction, items in (("UP", result.gainers), ("DOWN", result.losers)):
            for m in items[:limit // 2 + 1]:
                out.append({
                    "symbol": m.symbol, "direction": direction,
                    "change_pct": round(float(m.percent_change), 2),
                })
        return out
    except Exception as exc:
        log.debug("movers unavailable: %s", exc)
        return []
