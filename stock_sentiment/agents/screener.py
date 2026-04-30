"""ScreenerAgent — qualifies a single symbol on market.signal using static archetype thresholds.

Improvements over the original bot version:
  • Quality filters: price ≥ $5, avg daily volume ≥ 100 k shares
  • Time-of-day gate: no new signals in first 15 min or last 15 min of session
  • Relative strength: stock move vs its sector ETF (from MacroAgent)
  • Volume direction: volume spike on up-bar is bullish, down-bar is suspect
  • Macro-aware thresholds: RISK_OFF / PANIC require tighter RVOL + price move
  • Earnings lookahead: 7-day soft warning (score penalty) vs 3-day hard block
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

# Quality floors
_MIN_PRICE = 5.0
_MIN_AVG_VOLUME = 100_000

# Session gates (ET)
_MARKET_OPEN_HOUR, _MARKET_OPEN_MIN = 9, 30
_NO_ENTRY_OPEN_MIN = 15    # skip first 15 min (gap-fill noise)
_NO_ENTRY_CLOSE_MIN = 15   # skip last 15 min before 4 PM close

# Base thresholds
_T_BREAK_1W = 10.0
_T_BREAK_1M = 15.0
_T_MOMENTUM_3M = 7.0
_T_RECOVERY_DD = -15.0
_T_RECOVERY_BOUNCE = 4.0

# Macro-regime multipliers for RVOL / price-move gate
_REGIME_RVOL_MULT = {"RISK_ON": 1.0, "NEUTRAL": 1.0, "RISK_OFF": 1.3, "PANIC": 1.6}
_REGIME_MOVE_MULT = {"RISK_ON": 1.0, "NEUTRAL": 1.0, "RISK_OFF": 1.2, "PANIC": 1.5}


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

        # Time-of-day gate — skip price-discovery window and EOD noise
        now = datetime.now(_ET)
        minutes_since_open = (now.hour - _MARKET_OPEN_HOUR) * 60 + (now.minute - _MARKET_OPEN_MIN)
        minutes_to_close = (16 * 60) - (now.hour * 60 + now.minute)
        if minutes_since_open < _NO_ENTRY_OPEN_MIN:
            self.log.debug("Time gate (open): %s at %s", sym, now.strftime("%H:%M"))
            return
        if minutes_to_close < _NO_ENTRY_CLOSE_MIN:
            self.log.debug("Time gate (close): %s at %s", sym, now.strftime("%H:%M"))
            return

        loop = asyncio.get_running_loop()
        try:
            stock = await loop.run_in_executor(None, self._screen, sym, signal)
            if stock:
                self.log.info(
                    "Screened: %s [%s]  RS=%.1f%%  vol_dir=%s",
                    sym, stock.archetype,
                    stock.change_1w_pct,
                    signal.get("vol_direction", "?"),
                )
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

        from .macro import MacroAgent

        macro = MacroAgent.current
        regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"

        now = datetime.now(_ET).date()
        ticker = yf.Ticker(sym)
        df = ticker.history(period="3mo", auto_adjust=False)
        if df is None or len(df) < 20:
            return None

        df = df.dropna(subset=["Close", "High", "Low", "Volume"])
        closes = df["Close"].astype(float)
        highs = df["High"].astype(float)
        volumes = df["Volume"].astype(float)
        cur_p = float(closes.iloc[-1])
        avg_vol = float(volumes.tail(20).mean())

        # ---- Quality filters ----
        if cur_p < _MIN_PRICE:
            self.log.debug("Price filter: %s @ $%.2f", sym, cur_p)
            return None
        if avg_vol < _MIN_AVG_VOLUME:
            self.log.debug("Volume filter: %s avg=%.0f", sym, avg_vol)
            return None

        rvol = signal["rvol"]
        change_today = signal["price_change_pct"]

        # ---- Regime-adjusted RVOL / move thresholds ----
        # REEVAL signals re-check an existing position — skip entry-quality gates
        is_reeval = signal.get("trigger_type") == "REEVAL"
        if not is_reeval:
            rvol_min = 1.5 * _REGIME_RVOL_MULT.get(regime, 1.0)
            move_min = 2.0 * _REGIME_MOVE_MULT.get(regime, 1.0)
            if rvol < rvol_min or abs(change_today) < move_min:
                self.log.debug("Regime gate (%s): %s RVOL=%.1f move=%.1f%%", regime, sym, rvol, change_today)
                return None

        # ---- Volume direction: is the spike on an up or down bar? ----
        last_bar_up = float(closes.iloc[-1]) >= float(closes.iloc[-2]) if len(closes) >= 2 else True
        vol_direction = "UP" if last_bar_up else "DOWN"
        signal["vol_direction"] = vol_direction

        # Penalise bearish volume on a bullish signal and vice versa
        if change_today > 0 and vol_direction == "DOWN":
            rvol_min *= 1.2   # require 20% more RVOL when volume is on a down-bar during a price rise

        # ---- Earnings lookahead ----
        days_to_earnings = self._days_to_earnings(ticker, now)

        # ---- Momentum metrics ----
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

        # ---- Relative strength vs sector ----
        rel_strength = self._relative_strength(sym, change_today, macro)
        signal["rel_strength_vs_sector"] = rel_strength

        # ---- Archetype classification ----
        archetype: Optional[str] = None

        fresh_rvol = 2.0 * _REGIME_RVOL_MULT.get(regime, 1.0)
        if abs(change_today) >= 3.0 and rvol >= fresh_rvol and abs(change_1m) < 25.0:
            # Extra quality check: fresh breakout should show relative strength
            if rel_strength >= -1.0:  # not lagging its sector by >1%
                archetype = "FRESH_BREAKOUT"
            else:
                archetype = "BREAKOUT"  # move exists but sector is dragging it
        elif change_1w >= _T_BREAK_1W or change_1m >= _T_BREAK_1M:
            archetype = "BREAKOUT"
        elif drawdown <= _T_RECOVERY_DD and bounce >= _T_RECOVERY_BOUNCE and rvol > 1.1:
            archetype = "RECOVERY"
        elif change_3m >= _T_MOMENTUM_3M:
            archetype = "MOMENTUM"

        if archetype is None:
            self.log.debug(
                "No archetype: %s (1w=%+.1f%% 1m=%+.1f%% 3m=%+.1f%% dd=%.1f%% bounce=%.1f%%)",
                sym, change_1w, change_1m, change_3m, drawdown, bounce,
            )
            return None

        # ---- PANIC regime: only allow RECOVERY and high-conviction FRESH_BREAKOUT ----
        if regime == "PANIC" and archetype not in ("RECOVERY", "FRESH_BREAKOUT"):
            self.log.debug("Panic filter: dropping %s [%s]", sym, archetype)
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

    # ------------------------------------------------------------------
    # Relative strength vs sector ETF (uses MacroAgent's sector data)
    # ------------------------------------------------------------------

    @staticmethod
    def _relative_strength(sym: str, stock_change_pct: float, macro: dict) -> float:
        """Return stock_change - sector_change. Positive = outperforming sector."""
        if not macro:
            return 0.0
        from .portfolio import get_sector
        sector = get_sector(sym)
        sector_perf = macro.get("sector_performance", {})
        sector_change = sector_perf.get(sector, 0.0)
        return round(stock_change_pct - sector_change, 2)

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
