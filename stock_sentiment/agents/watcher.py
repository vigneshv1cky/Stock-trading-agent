"""WatcherAgent — streams 1-min bars from Alpaca WebSocket and fires market.signal events.

Signal gate: RVOL ≥ 1.5 AND |intraday_change| ≥ 2 %
Debounce:    5-min per-symbol cooldown prevents pipeline spam.
Fallback:    yfinance 1-min poll every 90 s when no Alpaca keys are set.
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus

_RVOL_THRESHOLD = 1.5
_PRICE_MOVE_THRESHOLD = 2.0   # % intraday
_SIGNAL_COOLDOWN_S = 300      # 5 min per symbol
_TRADING_MINUTES = 390.0      # 6.5 h × 60


class WatcherAgent(BaseAgent):
    def __init__(self, bus: EventBus, symbols: list[str]):
        super().__init__(bus, "WatcherAgent")
        self.symbols = symbols
        self._avg_volumes: dict[str, float] = {}
        self._daily_open: dict[str, float] = {}
        self._cumulative_vol: dict[str, float] = {}
        self._session_date: str = ""
        self._last_signal: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self._preload_avg_volumes()
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if api_key and secret:
            await self._stream_bars(api_key, secret)
        else:
            self.log.warning("No Alpaca keys — using yfinance poll fallback (90 s interval)")
            await self._poll_fallback()

    # ------------------------------------------------------------------
    # Volume pre-load
    # ------------------------------------------------------------------

    async def _preload_avg_volumes(self) -> None:
        self.log.info("Preloading 20-day avg volumes for %d symbols…", len(self.symbols))
        loop = asyncio.get_running_loop()
        try:
            import yfinance as yf

            data = await loop.run_in_executor(
                None,
                lambda: yf.download(
                    self.symbols, period="1mo", progress=False,
                    threads=True, auto_adjust=False,
                ),
            )
            for sym in self.symbols:
                try:
                    vols = data["Volume"][sym].dropna()
                    if len(vols) >= 5:
                        self._avg_volumes[sym] = float(vols.tail(20).mean())
                except Exception:
                    pass
            self.log.info("Avg volumes loaded for %d symbols", len(self._avg_volumes))
        except Exception as exc:
            self.log.error("Volume preload failed: %s", exc)

    # ------------------------------------------------------------------
    # Session reset (new trading day)
    # ------------------------------------------------------------------

    def _reset_session_if_needed(self) -> None:
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        if today != self._session_date:
            self._session_date = today
            self._daily_open.clear()
            self._cumulative_vol.clear()

    # ------------------------------------------------------------------
    # Alpaca WebSocket path
    # ------------------------------------------------------------------

    async def _stream_bars(self, api_key: str, secret: str) -> None:
        import threading
        from alpaca.data.live import StockDataStream  # type: ignore[import-untyped]

        stream = StockDataStream(api_key, secret)

        async def bar_handler(bar) -> None:  # type: ignore[no-untyped-def]
            await self._process_bar(bar.symbol, float(bar.close), float(bar.volume))

        stream.subscribe_bars(bar_handler, *self.symbols)
        self.log.info("Alpaca bar stream started (%d symbols)", len(self.symbols))

        # Daemon thread — won't block process exit when cancelled
        t = threading.Thread(target=stream.run, daemon=True)
        t.start()
        try:
            while t.is_alive():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            stream.stop()
            raise

    # ------------------------------------------------------------------
    # Bar processing (shared by WebSocket and fallback paths)
    # ------------------------------------------------------------------

    async def _process_bar(self, sym: str, price: float, volume: float) -> None:
        self._reset_session_if_needed()

        if sym not in self._daily_open:
            self._daily_open[sym] = price
        self._cumulative_vol[sym] = self._cumulative_vol.get(sym, 0.0) + volume

        avg_daily = self._avg_volumes.get(sym, 0.0)
        if avg_daily <= 0:
            return

        now_et = datetime.now(_ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_elapsed = max(1.0, (now_et - market_open).total_seconds() / 60.0)
        expected_vol = avg_daily * (minutes_elapsed / _TRADING_MINUTES)
        rvol = self._cumulative_vol[sym] / expected_vol if expected_vol > 0 else 0.0

        open_price = self._daily_open[sym]
        price_change_pct = ((price - open_price) / open_price * 100.0) if open_price > 0 else 0.0

        if rvol < _RVOL_THRESHOLD or abs(price_change_pct) < _PRICE_MOVE_THRESHOLD:
            return

        if time.time() - self._last_signal.get(sym, 0.0) < _SIGNAL_COOLDOWN_S:
            return
        self._last_signal[sym] = time.time()

        trigger = (
            "FRESH_BREAKOUT" if abs(price_change_pct) >= 3.0 and rvol >= 2.0
            else "VOLUME_SPIKE"
        )
        self.log.info(
            "Signal: %s  RVOL=%.1fx  price=%+.1f%%  [%s]", sym, rvol, price_change_pct, trigger
        )
        await self.bus.publish("market.signal", {
            "symbol": sym,
            "price": price,
            "rvol": rvol,
            "price_change_pct": price_change_pct,
            "trigger_type": trigger,
            "timestamp": now_et.isoformat(),
        })

    # ------------------------------------------------------------------
    # yfinance fallback (no Alpaca keys)
    # ------------------------------------------------------------------

    async def _poll_fallback(self) -> None:
        import yfinance as yf

        loop = asyncio.get_running_loop()
        chunk = self.symbols[:100]  # yfinance bulk limit

        while True:
            await asyncio.sleep(90)
            self._reset_session_if_needed()
            try:
                data = await loop.run_in_executor(
                    None,
                    lambda: yf.download(
                        chunk, period="1d", interval="1m",
                        progress=False, auto_adjust=False, threads=True,
                    ),
                )
                for sym in chunk:
                    try:
                        df = data[sym].dropna(subset=["Close", "Volume"])
                        if df.empty:
                            continue
                        latest = df.iloc[-1]
                        await self._process_bar(sym, float(latest["Close"]), float(latest["Volume"]))
                    except Exception:
                        continue
            except Exception as exc:
                self.log.error("Poll fallback error: %s", exc)
