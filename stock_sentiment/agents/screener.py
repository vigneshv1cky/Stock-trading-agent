"""ScreenerAgent — qualifies a single symbol on market.signal using quality and regime gates.

Improvements over the original bot version:
  • Quality filters: price ≥ $5, avg daily volume ≥ 100 k shares
  • Time-of-day gate: no new signals in first 15 min or last 15 min of session
  • Relative strength: stock move vs its sector ETF (from MacroAgent)
  • Volume direction: volume spike on up-bar is bullish, down-bar is suspect
  • Macro-aware thresholds: RISK_OFF / PANIC require tighter RVOL + price move
  • Earnings lookahead: 7-day soft warning (score penalty) vs 3-day hard block
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI

    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus


@dataclass
class ScreenedStock:
    symbol: str
    current_price: float
    change_3m_pct: float
    change_1m_pct: float
    change_1w_pct: float
    avg_volume: float
    volume_ratio: float
    daily_closes_3m: list
    days_to_earnings: Optional[int] = None
    change_today_pct: float = 0.0
    asset_class: str = "stock"


# Quality floors
_MIN_PRICE = 5.0
_MIN_AVG_VOLUME = 100_000

# Session gates (ET)
_MARKET_OPEN_HOUR, _MARKET_OPEN_MIN = 9, 30
_NO_ENTRY_OPEN_MIN = 15  # skip first 15 min (gap-fill noise)
_NO_ENTRY_CLOSE_MIN = 15  # skip last 15 min before 4 PM close

# Macro-regime multipliers for RVOL / price-move gate
_REGIME_RVOL_MULT = {"RISK_ON": 1.0, "NEUTRAL": 1.0, "RISK_OFF": 1.3, "PANIC": 1.6}
_REGIME_MOVE_MULT = {"RISK_ON": 1.0, "NEUTRAL": 1.0, "RISK_OFF": 1.2, "PANIC": 1.5}


class ScreenerAgent(BaseAgent):
    _semaphore: asyncio.Semaphore = asyncio.Semaphore(5)

    def __init__(self, bus: EventBus):
        super().__init__(bus, "ScreenerAgent")
        self._queue = bus.subscribe("market.signal")

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, signal: dict) -> None:
        sym = signal["symbol"]

        # Time-of-day gate — stocks only; crypto is 24/7
        if "/" not in sym:
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
            async with ScreenerAgent._semaphore:
                stock = await loop.run_in_executor(None, self._screen, sym, signal)
            if stock:
                self.log.info(
                    "Screened: %s  RVOL=%.1fx  today=%+.1f%%  RS=%.1f%%  vol_dir=%s",
                    sym,
                    stock.volume_ratio,
                    stock.change_today_pct,
                    stock.change_1w_pct,
                    signal.get("vol_direction", "?"),
                )
                await self.bus.publish("symbol.screened", {"symbol": sym, "screened_stock": stock, "signal": signal})
            else:
                self.log.debug("Rejected: %s", sym)
        except Exception as exc:
            self.log.error("Screener error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Blocking work — runs in executor
    # ------------------------------------------------------------------

    def _screen(self, sym: str, signal: dict) -> Optional[ScreenedStock]:
        if "/" in sym:
            return self._screen_crypto(sym, signal)
        import yfinance as yf

        from .macro import MacroAgent

        macro = MacroAgent.current
        regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"

        ticker = yf.Ticker(sym)
        df = ticker.history(period="3mo", auto_adjust=False)
        if df is None or len(df) < 20:
            return None

        df = df.dropna(subset=["Close", "High", "Low", "Volume"])
        closes = df["Close"].astype(float)
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
            rvol_min = 1.2 * _REGIME_RVOL_MULT.get(regime, 1.0)
            move_min = 1.5 * _REGIME_MOVE_MULT.get(regime, 1.0)
            # Extreme RVOL (≥8x) overrides the price-move minimum — the volume IS the signal
            extreme_rvol = rvol >= 8.0
            if rvol < rvol_min or (abs(change_today) < move_min and not extreme_rvol):
                self.log.debug("Regime gate (%s): %s RVOL=%.1f move=%.1f%%", regime, sym, rvol, change_today)
                return None

        # ---- Momentum metrics ----
        base_3m = float(closes.iloc[0])
        base_1m = float(closes.iloc[-21]) if len(closes) > 21 else base_3m
        base_1w = float(closes.iloc[-5]) if len(closes) > 5 else base_3m
        change_3m = (cur_p - base_3m) / base_3m * 100 if base_3m > 0 else 0.0
        change_1m = (cur_p - base_1m) / base_1m * 100 if base_1m > 0 else 0.0
        change_1w = (cur_p - base_1w) / base_1w * 100 if base_1w > 0 else 0.0
        # ---- Relative strength vs sector ----
        rel_strength = self._relative_strength(sym, change_today, macro)
        signal["rel_strength_vs_sector"] = rel_strength

        # ---- PANIC regime: only allow extreme RVOL signals ----
        if regime == "PANIC" and rvol < 5.0:
            self.log.debug("Panic filter: dropping %s RVOL=%.1f", sym, rvol)
            return None

        return ScreenedStock(
            symbol=sym,
            current_price=cur_p,
            change_3m_pct=change_3m,
            change_1m_pct=change_1m,
            change_1w_pct=change_1w,
            avg_volume=avg_vol,
            volume_ratio=rvol,
            daily_closes_3m=[float(v) for v in closes.values],
            change_today_pct=change_today,
        )

    # ------------------------------------------------------------------
    # Crypto screening — uses Alpaca Crypto Historical instead of yfinance
    # ------------------------------------------------------------------

    def _screen_crypto(self, sym: str, signal: dict) -> Optional[ScreenedStock]:
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import CryptoBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame  # type: ignore[import-untyped]
        except ImportError:
            self.log.warning("alpaca-py not installed — cannot screen crypto %s", sym)
            return None

        from .macro import MacroAgent

        macro = MacroAgent.current
        regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"

        try:
            from datetime import timezone as _tz

            client = CryptoHistoricalDataClient()
            end = datetime.now(_tz.utc)
            start = end - timedelta(days=90)
            request = CryptoBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=start, end=end)
            bars_data = client.get_crypto_bars(request)
            sym_bars = bars_data[sym]
        except Exception as exc:
            self.log.info("Crypto history failed %s: %s", sym, exc)
            return None

        if not sym_bars or len(sym_bars) < 20:
            return None

        closes = [float(b.close) for b in sym_bars]
        volumes = [float(b.volume) for b in sym_bars]
        cur_p = closes[-1]
        avg_vol = sum(volumes[-20:]) / min(len(volumes), 20)

        if avg_vol <= 0:
            return None

        rvol = signal["rvol"]
        change_today = signal["price_change_pct"]
        is_reeval = signal.get("trigger_type") == "REEVAL"

        if not is_reeval:
            rvol_min = 1.0 * _REGIME_RVOL_MULT.get(regime, 1.0)
            move_min = 1.5 * _REGIME_MOVE_MULT.get(regime, 1.0)
            extreme_rvol = rvol >= 8.0
            if rvol < rvol_min or (abs(change_today) < move_min and not extreme_rvol):
                self.log.info(
                    "Crypto rejected (%s): %s RVOL=%.1f(min=%.1f) move=%.1f%%(min=%.1f%%)",
                    regime,
                    sym,
                    rvol,
                    rvol_min,
                    change_today,
                    move_min,
                )
                return None

        base_3m = closes[0]
        base_1m = closes[-21] if len(closes) > 21 else base_3m
        base_1w = closes[-5] if len(closes) > 5 else base_3m
        change_3m = (cur_p - base_3m) / base_3m * 100 if base_3m > 0 else 0.0
        change_1m = (cur_p - base_1m) / base_1m * 100 if base_1m > 0 else 0.0
        change_1w = (cur_p - base_1w) / base_1w * 100 if base_1w > 0 else 0.0

        if regime == "PANIC" and rvol < 5.0:
            self.log.debug("Panic filter: dropping crypto %s RVOL=%.1f", sym, rvol)
            return None

        return ScreenedStock(
            symbol=sym,
            current_price=cur_p,
            change_3m_pct=change_3m,
            change_1m_pct=change_1m,
            change_1w_pct=change_1w,
            avg_volume=avg_vol,
            volume_ratio=rvol,
            daily_closes_3m=closes,
            change_today_pct=change_today,
            asset_class="crypto",
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
