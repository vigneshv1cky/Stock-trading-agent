"""ScreenerAgent — qualifies a single symbol on market.signal using static archetype thresholds.

Adaptive percentile thresholds from the batch screener don't apply to one symbol,
so we use the static fallback values that screener.py already defines.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from stock_sentiment.market.screener import ScreenedStock

from .base import BaseAgent
from .event_bus import EventBus

# Static thresholds (mirrors screener.py's <10-stock fallback)
_T_BREAK_1W = 10.0
_T_BREAK_1M = 15.0
_T_MOMENTUM_3M = 7.0
_T_RECOVERY_DD = -15.0
_T_RECOVERY_BOUNCE = 4.0


class ScreenerAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "ScreenerAgent")
        self._queue = bus.subscribe("market.signal")

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, signal: dict) -> None:
        sym = signal["symbol"]
        loop = asyncio.get_event_loop()
        try:
            stock = await loop.run_in_executor(None, self._screen, sym, signal)
            if stock:
                self.log.info("Screened: %s [%s]", sym, stock.archetype)
                await self.bus.publish("symbol.screened", {
                    "symbol": sym,
                    "screened_stock": stock,
                    "signal": signal,
                })
            else:
                self.log.debug("Rejected: %s", sym)
        except Exception as exc:
            self.log.error("Screener error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Blocking work — runs in executor
    # ------------------------------------------------------------------

    def _screen(self, sym: str, signal: dict) -> Optional[ScreenedStock]:
        import yfinance as yf

        now = datetime.now(_ET).date()
        ticker = yf.Ticker(sym)
        df = ticker.history(period="3mo", auto_adjust=False)
        if df is None or len(df) < 20:
            return None

        df = df.dropna(subset=["Close", "High", "Volume"])
        closes = df["Close"].astype(float)
        highs = df["High"].astype(float)
        volumes = df["Volume"].astype(float)
        cur_p = float(closes.iloc[-1])
        avg_vol = float(volumes.tail(20).mean())
        rvol = signal["rvol"]

        # Earnings blackout
        days_to_earnings = self._days_to_earnings(ticker, now)
        if days_to_earnings is not None and days_to_earnings <= 3:
            self.log.debug("Earnings blackout: %s (%dd)", sym, days_to_earnings)
            return None

        # Momentum metrics
        base_3m = float(closes.iloc[0])
        base_1m = float(closes.iloc[-21]) if len(closes) > 21 else base_3m
        base_1w = float(closes.iloc[-5]) if len(closes) > 5 else base_3m
        change_3m = (cur_p - base_3m) / base_3m * 100 if base_3m > 0 else 0.0
        change_1m = (cur_p - base_1m) / base_1m * 100 if base_1m > 0 else 0.0
        change_1w = (cur_p - base_1w) / base_1w * 100 if base_1w > 0 else 0.0
        max_3m = float(highs.max())
        drawdown = (cur_p - max_3m) / max_3m * 100 if max_3m > 0 else 0.0
        base_3d = float(closes.iloc[-3]) if len(closes) > 3 else cur_p
        bounce = (cur_p - base_3d) / base_3d * 100 if base_3d > 0 else 0.0
        change_today = signal["price_change_pct"]

        archetype: Optional[str] = None
        if abs(change_today) >= 3.0 and rvol >= 2.0 and abs(change_1m) < 25.0:
            archetype = "FRESH_BREAKOUT"
        elif change_1w >= _T_BREAK_1W or change_1m >= _T_BREAK_1M:
            archetype = "BREAKOUT"
        elif drawdown <= _T_RECOVERY_DD and bounce >= _T_RECOVERY_BOUNCE and rvol > 1.1:
            archetype = "RECOVERY"
        elif change_3m >= _T_MOMENTUM_3M:
            archetype = "MOMENTUM"

        if archetype is None:
            return None

        return ScreenedStock(
            symbol=sym,
            current_price=cur_p,
            change_3m_pct=change_3m,
            change_1m_pct=change_1m,
            change_1w_pct=change_1w,
            avg_volume=avg_vol,
            volume_ratio=rvol,
            archetype=archetype,
            daily_closes_3m=[float(v) for v in closes.values],
            days_to_earnings=days_to_earnings,
            change_today_pct=change_today,
        )

    @staticmethod
    def _days_to_earnings(ticker, today) -> Optional[int]:
        try:
            cal = ticker.calendar
            dates: list = []
            if isinstance(cal, dict) and "Earnings Date" in cal:
                dates = list(cal["Earnings Date"])
            elif isinstance(cal, pd.DataFrame):
                try:
                    raw = cal.loc["Earnings Date"]
                    dates = list(raw.values) if hasattr(raw, "values") else [raw]
                except Exception:
                    pass
            if not dates:
                return None
            future = [
                (dt.date() if hasattr(dt, "date") else dt)
                for dt in dates
                if (dt.date() if hasattr(dt, "date") else dt) >= today
            ]
            if future:
                return (future[0] - today).days
        except Exception:
            pass
        return None
