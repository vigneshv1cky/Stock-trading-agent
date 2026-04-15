"""Fetches and caches stock/ETF price data via yfinance."""

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from rich.console import Console

console = Console()


@dataclass
class PriceData:
    symbol: str
    current_price: float
    change_pct: float  # Daily % change
    volume: int
    avg_volume_20d: float
    ohlcv: pd.DataFrame  # 30-day history
    fetched_at: datetime


class PriceFetcher:
    """Fetches and caches stock/ETF price data via yfinance."""

    def __init__(self, cache_ttl_seconds: int = 900):
        self._cache: dict[str, PriceData] = {}
        self._cache_ttl = cache_ttl_seconds

    def fetch_batch(
        self, symbols: list[str], period: str = "1mo"
    ) -> dict[str, PriceData]:
        """Fetch price data for multiple symbols in a single yfinance call."""
        # Check cache first
        uncached = [s for s in symbols if not self._is_cached(s)]
        cached_results = {
            s: self._cache[s] for s in symbols if self._is_cached(s)
        }

        if not uncached:
            return cached_results

        try:
            import yfinance as yf

            console.print(
                f"[cyan]Fetching prices for {len(uncached)} symbols...[/cyan]"
            )

            # Batch download - single HTTP request
            data = yf.download(
                uncached,
                period=period,
                group_by="ticker" if len(uncached) > 1 else "column",
                progress=False,
                threads=True,
            )

            if data.empty:
                return cached_results

            now = datetime.now(timezone.utc)

            for symbol in uncached:
                try:
                    if len(uncached) == 1:
                        df = data.copy()
                    else:
                        df = data[symbol].copy()

                    df = df.dropna(how="all")
                    if df.empty or len(df) < 2:
                        continue

                    # Flatten MultiIndex columns if present
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)

                    current = float(df["Close"].iloc[-1])
                    prev = float(df["Close"].iloc[-2])
                    change_pct = ((current - prev) / prev) * 100

                    vol = int(df["Volume"].iloc[-1]) if not pd.isna(df["Volume"].iloc[-1]) else 0
                    avg_vol = float(df["Volume"].tail(20).mean()) if len(df) >= 5 else float(vol)

                    price_data = PriceData(
                        symbol=symbol,
                        current_price=current,
                        change_pct=change_pct,
                        volume=vol,
                        avg_volume_20d=avg_vol,
                        ohlcv=df,
                        fetched_at=now,
                    )
                    self._cache[symbol] = price_data
                except Exception:
                    continue

        except Exception as e:
            console.print(f"[yellow]Warning: Price fetch failed: {e}[/yellow]")

        # Merge cached + newly fetched
        result = dict(cached_results)
        for s in uncached:
            if s in self._cache:
                result[s] = self._cache[s]

        return result

    def _is_cached(self, symbol: str) -> bool:
        if symbol not in self._cache:
            return False
        elapsed = (
            datetime.now(timezone.utc) - self._cache[symbol].fetched_at
        ).total_seconds()
        return elapsed < self._cache_ttl
