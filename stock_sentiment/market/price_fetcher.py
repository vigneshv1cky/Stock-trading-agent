"""Fetches and caches stock price data.

Price/volume/OHLCV: Alpaca StockHistoricalDataClient (official API, no rate-limit risk).
Earnings calendar: yfinance Ticker.calendar, cached per-symbol for 24 hours so
  the 240-symbol yfinance scan runs at most once per day regardless of how many
  30-min screener cycles fire.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from rich.console import Console

console = Console()

_PERIOD_TO_DAYS: dict[str, int] = {
    "1mo": 45,
    "3mo": 100,
    "6mo": 195,
    "1y": 380,
}
_EARNINGS_TTL_S = 86_400  # 24 hours — earnings dates change rarely


@dataclass
class PriceData:
    symbol: str
    current_price: float
    change_pct: float
    volume: int
    avg_volume_20d: float
    ohlcv: pd.DataFrame
    fetched_at: datetime
    days_to_earnings: Optional[int] = None


class PriceFetcher:
    def __init__(self, cache_ttl_seconds: int = 900):
        self._cache: dict[str, PriceData] = {}
        self._cache_ttl = cache_ttl_seconds
        # Earnings: lazily fetched per symbol, refreshed at most once per 24h
        self._earnings_cache: dict[str, Optional[int]] = {}
        self._earnings_ts: dict[str, datetime] = {}
        self._alpaca: object = None  # StockHistoricalDataClient, lazy init

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_batch(self, symbols: list[str], period: str = "1mo") -> dict[str, PriceData]:
        uncached = [s for s in symbols if not self._is_cached(s)]
        result = {s: self._cache[s] for s in symbols if self._is_cached(s)}

        if result:
            print(f"[PriceFetcher] Cache hit: {len(result)}/{len(symbols)} symbols")
        if uncached:
            print(f"[PriceFetcher] Fetching {len(uncached)} uncached symbols (period={period})...")
            self._fetch_alpaca(uncached, period)
            fetched = [s for s in uncached if s in self._cache]
            failed = [s for s in uncached if s not in self._cache]
            print(f"[PriceFetcher] Alpaca fetch complete: {len(fetched)} OK, {len(failed)} failed")
            if failed:
                print(f"[PriceFetcher] No data for: {failed}")
            for s in uncached:
                if s in self._cache:
                    result[s] = self._cache[s]

        print(f"[PriceFetcher] Returning data for {len(result)}/{len(symbols)} symbols")
        return result

    # ------------------------------------------------------------------
    # Alpaca price fetch
    # ------------------------------------------------------------------

    def _fetch_alpaca(self, symbols: list[str], period: str) -> None:
        try:
            from alpaca.data.enums import Adjustment
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            days = _PERIOD_TO_DAYS.get(period, 100)
            start = datetime.now(timezone.utc) - timedelta(days=days)

            console.print(f"[cyan]Fetching prices for {len(symbols)} symbols via Alpaca...[/cyan]")
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                adjustment=Adjustment.SPLIT,
            )
            bars = self._get_alpaca_client().get_stock_bars(request)
        except Exception as e:
            console.print(f"[yellow]Warning: Alpaca price fetch failed: {e}[/yellow]")
            return

        now = datetime.now(timezone.utc)

        for symbol in symbols:
            try:
                symbol_bars = bars[symbol]
                if not symbol_bars or len(symbol_bars) < 2:
                    print(f"[PriceFetcher] {symbol}: insufficient bars ({len(symbol_bars) if symbol_bars else 0}), skipping")
                    continue

                df = pd.DataFrame([
                    {
                        "Open": float(b.open),
                        "High": float(b.high),
                        "Low": float(b.low),
                        "Close": float(b.close),
                        "Volume": int(b.volume),
                    }
                    for b in symbol_bars
                ])
                df.index = pd.DatetimeIndex([b.timestamp for b in symbol_bars])

                current = float(df["Close"].iloc[-1])
                prev = float(df["Close"].iloc[-2])
                change_pct = ((current - prev) / prev) * 100

                vol_series = df["Volume"]
                if len(vol_series) >= 22:
                    ref_vol = int(vol_series.iloc[-2])
                    avg_vol = float(vol_series.iloc[-22:-2].mean())
                else:
                    ref_vol = int(vol_series.iloc[-1])
                    avg_vol = float(vol_series.mean())

                self._cache[symbol] = PriceData(
                    symbol=symbol,
                    current_price=current,
                    change_pct=change_pct,
                    volume=ref_vol,
                    avg_volume_20d=avg_vol,
                    ohlcv=df,
                    fetched_at=now,
                    days_to_earnings=self._get_earnings(symbol),
                )
            except Exception as e:
                print(f"[PriceFetcher] {symbol}: parse error — {e}")
                continue

    # ------------------------------------------------------------------
    # Earnings calendar — yfinance only, 24h per-symbol TTL
    # ------------------------------------------------------------------

    def _get_earnings(self, symbol: str) -> Optional[int]:
        now = datetime.now(timezone.utc)
        last = self._earnings_ts.get(symbol)
        if last is not None and (now - last).total_seconds() < _EARNINGS_TTL_S:
            return self._earnings_cache.get(symbol)

        days: Optional[int] = None
        try:
            import yfinance as yf
            cal = yf.Ticker(symbol).calendar
            if isinstance(cal, dict) and "Earnings Date" in cal:
                dates = cal.get("Earnings Date", [])
                normalized = [dt.date() if hasattr(dt, "date") else dt for dt in dates]
                future = [dt for dt in normalized if dt >= now.date()]
                if future:
                    days = (future[0] - now.date()).days
        except Exception:
            pass

        self._earnings_cache[symbol] = days
        self._earnings_ts[symbol] = now
        return days

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_cached(self, symbol: str) -> bool:
        entry = self._cache.get(symbol)
        if entry is None:
            return False
        return (datetime.now(timezone.utc) - entry.fetched_at).total_seconds() < self._cache_ttl

    def _get_alpaca_client(self):
        if self._alpaca is None:
            api_key = os.environ.get("ALPACA_API_KEY", "")
            secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
            if not api_key or not secret_key:
                print("[PriceFetcher] WARNING: ALPACA_API_KEY or ALPACA_SECRET_KEY not set — fetch will fail!")
            print("[PriceFetcher] Initializing Alpaca StockHistoricalDataClient...")
            from alpaca.data.historical import StockHistoricalDataClient
            self._alpaca = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
            print("[PriceFetcher] Alpaca client initialized OK")
        return self._alpaca
