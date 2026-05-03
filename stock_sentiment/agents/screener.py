"""ScreenerAgent — qualifies a single symbol on market.signal using quality gates.

  • Quality filters: price ≥ $5, avg daily volume ≥ 100 k shares
  • Time-of-day gate: no new signals in first 15 min or last 15 min of session
  • Signal gate: RVOL ≥ 1.5, |price change| ≥ 2.0% for stocks; RVOL ≥ 1.2, |price change| ≥ 2.5% for crypto (extreme RVOL ≥ 8x bypasses move gate)
  • Earnings lookahead: soft context for LLM
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


class ScreenerAgent(BaseAgent):
    _semaphore: asyncio.Semaphore = asyncio.Semaphore(5)
    _last_screened: dict[str, float] = {}
    _SCREEN_COOLDOWN_S: float = 300.0  # 5 min per symbol (non-REEVAL)

    def __init__(self, bus: EventBus):
        super().__init__(bus, "ScreenerAgent")
        self._queue = bus.subscribe("market.signal")

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, signal: dict) -> None:
        sym = signal["symbol"]

        # Dedup: skip re-screening the same symbol within 5 min (Watcher+Scanner fire same symbols)
        if signal.get("trigger_type") != "REEVAL":
            now_mono = asyncio.get_event_loop().time()
            last = ScreenerAgent._last_screened.get(sym, 0.0)
            if now_mono - last < ScreenerAgent._SCREEN_COOLDOWN_S:
                self.log.info("Dedup skip: %s (%.0fs ago)", sym, now_mono - last)
                return
            ScreenerAgent._last_screened[sym] = now_mono

        # Time-of-day gate — stocks only; crypto is 24/7
        if "/" not in sym:
            now = datetime.now(_ET)
            minutes_since_open = (now.hour - _MARKET_OPEN_HOUR) * 60 + (now.minute - _MARKET_OPEN_MIN)
            minutes_to_close = (16 * 60) - (now.hour * 60 + now.minute)
            if minutes_since_open < _NO_ENTRY_OPEN_MIN:
                self.log.info("Time gate (open): %s at %s", sym, now.strftime("%H:%M"))
                return
            if minutes_to_close < _NO_ENTRY_CLOSE_MIN:
                self.log.info("Time gate (close): %s at %s", sym, now.strftime("%H:%M"))
                return

        loop = asyncio.get_running_loop()
        try:
            async with ScreenerAgent._semaphore:
                stock = await loop.run_in_executor(None, self._screen, sym, signal)
            if stock:
                self.log.info(
                    "Screened ✓: %s  RVOL=%.1fx  today=%+.1f%%  vol_dir=%s  trigger=%s",
                    sym,
                    stock.volume_ratio,
                    stock.change_today_pct,
                    signal.get("vol_direction", "?"),
                    signal.get("trigger_type", "WATCH"),
                )
                await self.bus.publish("symbol.screened", {"symbol": sym, "screened_stock": stock, "signal": signal})
            else:
                self.log.info("Screened ✗: %s  RVOL=%.1fx  today=%+.1f%%  (below signal thresholds)",
                              sym, signal.get("rvol", 0), signal.get("price_change_pct", 0))
        except Exception as exc:
            self.log.error("Screener error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Blocking work — runs in executor
    # ------------------------------------------------------------------

    def _screen(self, sym: str, signal: dict) -> Optional[ScreenedStock]:
        if "/" in sym:
            return self._screen_crypto(sym, signal)
        import yfinance as yf

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

        trigger = signal.get("trigger_type", "")
        is_reeval = trigger == "REEVAL"
        is_news = trigger == "NEWS_CATALYST"

        rvol = signal["rvol"]
        change_today = signal["price_change_pct"]

        # NEWS_CATALYST: compute real intraday metrics; skip if price already moved
        # (a moved stock would be caught by WatcherAgent instead)
        if is_news:
            snapshot = self._fetch_intraday_snapshot(sym)
            if snapshot:
                open_p, cur_p_intra, day_vol = snapshot
                if open_p > 0:
                    change_today = (cur_p_intra - open_p) / open_p * 100
                if abs(change_today) >= 1.5:
                    self.log.debug("NEWS_CATALYST skip %s: already moved %+.1f%%",
                                   sym, change_today)
                    return None
                now_et = datetime.now(_ET)
                minutes_elapsed = max(1, (now_et.hour - 9) * 60 + (now_et.minute - 30))
                expected_vol = avg_vol * (minutes_elapsed / 390.0)
                rvol = day_vol / expected_vol if expected_vol > 0 else 0.0

        # REEVAL and NEWS_CATALYST bypass the entry signal gate
        if not is_reeval and not is_news:
            # Extreme RVOL (≥8x) overrides the price-move minimum — the volume IS the signal
            extreme_rvol = rvol >= 8.0
            if rvol < 1.5 or (abs(change_today) < 2.0 and not extreme_rvol):
                self.log.debug("Signal gate: %s RVOL=%.1f move=%.1f%%", sym, rvol, change_today)
                return None

        # ---- Momentum metrics ----
        base_3m = float(closes.iloc[0])
        base_1m = float(closes.iloc[-21]) if len(closes) > 21 else base_3m
        base_1w = float(closes.iloc[-5]) if len(closes) > 5 else base_3m
        change_3m = (cur_p - base_3m) / base_3m * 100 if base_3m > 0 else 0.0
        change_1m = (cur_p - base_1m) / base_1m * 100 if base_1m > 0 else 0.0
        change_1w = (cur_p - base_1w) / base_1w * 100 if base_1w > 0 else 0.0

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

    @staticmethod
    def _fetch_intraday_snapshot(sym: str) -> Optional[tuple[float, float, float]]:
        """Returns (open_price, current_price, cumulative_volume) from today's 1-min bars."""
        try:
            import yfinance as yf
            df = yf.Ticker(sym).history(period="1d", interval="1m")
            if df is None or len(df) < 2:
                return None
            opens = df["Open"].astype(float)
            closes = df["Close"].astype(float)
            volumes = df["Volume"].astype(float)
            return float(opens.iloc[0]), float(closes.iloc[-1]), float(volumes.sum())
        except Exception:
            return None

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
            extreme_rvol = rvol >= 8.0
            if rvol < 1.2 or (abs(change_today) < 2.5 and not extreme_rvol):
                self.log.info("Crypto rejected: %s RVOL=%.1f move=%.1f%%", sym, rvol, change_today)
                return None

        base_3m = closes[0]
        base_1m = closes[-21] if len(closes) > 21 else base_3m
        base_1w = closes[-5] if len(closes) > 5 else base_3m
        change_3m = (cur_p - base_3m) / base_3m * 100 if base_3m > 0 else 0.0
        change_1m = (cur_p - base_1m) / base_1m * 100 if base_1m > 0 else 0.0
        change_1w = (cur_p - base_1w) / base_1w * 100 if base_1w > 0 else 0.0

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

