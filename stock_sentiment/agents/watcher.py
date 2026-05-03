"""WatcherAgent — streams 1-min bars from Alpaca WebSocket and fires market.signal events.

Signal gate: RVOL ≥ 2.0 AND |intraday_change| ≥ 2.0%
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

_RVOL_THRESHOLD = 2.0
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
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if api_key and secret:
            await self._preload_volumes_alpaca(api_key, secret)
        else:
            await self._preload_volumes_yfinance()
        await self._preload_session_state()

    async def _preload_volumes_alpaca(self, api_key: str, secret: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            from alpaca.data.enums import DataFeed  # type: ignore[import-untyped]
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import StockBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame  # type: ignore[import-untyped]

            client = StockHistoricalDataClient(api_key, secret)
            end = datetime.now(_ET)
            start = end - timedelta(days=30)
            request = StockBarsRequest(
                symbol_or_symbols=self.symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
            )
            bars = await loop.run_in_executor(None, client.get_stock_bars, request)
            for sym in self.symbols:
                try:
                    sym_bars = bars[sym]
                    if sym_bars and len(sym_bars) >= 5:
                        vols = [b.volume for b in sym_bars[-20:]]
                        self._avg_volumes[sym] = float(sum(vols) / len(vols))
                except Exception:
                    pass
            self.log.info("Avg volumes loaded for %d symbols (Alpaca)", len(self._avg_volumes))
        except Exception as exc:
            self.log.warning("Alpaca volume preload failed: %s — falling back to yfinance", exc)
            await self._preload_volumes_yfinance()

    async def _preload_volumes_yfinance(self) -> None:
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
            self.log.info("Avg volumes loaded for %d symbols (yfinance)", len(self._avg_volumes))
        except Exception as exc:
            self.log.error("Volume preload failed: %s", exc)

    async def _preload_session_state(self) -> None:
        """Load the true 9:30 AM open and cumulative volume from today's 1-min bars."""
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if api_key and secret:
            await self._preload_session_alpaca(api_key, secret)
        else:
            await self._preload_session_yfinance()

    async def _preload_session_alpaca(self, api_key: str, secret: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            from alpaca.data.enums import DataFeed  # type: ignore[import-untyped]
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import StockBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore[import-untyped]

            client = StockHistoricalDataClient(api_key, secret)
            now = datetime.now(_ET)
            today = now.strftime("%Y-%m-%d")
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            request = StockBarsRequest(
                symbol_or_symbols=self.symbols,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=market_open,
                end=now,
                feed=DataFeed.IEX,
            )
            bars = await loop.run_in_executor(None, client.get_stock_bars, request)
            loaded = 0
            for sym in self.symbols:
                try:
                    sym_bars = bars[sym]
                    if sym_bars:
                        self._daily_open[sym] = float(sym_bars[0].open)
                        self._cumulative_vol[sym] = float(sum(b.volume for b in sym_bars))
                        loaded += 1
                except Exception:
                    pass
            self._session_date = today
            self.log.info("Session state preloaded for %d symbols (Alpaca)", loaded)
        except Exception as exc:
            self.log.warning("Alpaca session preload failed: %s — falling back to yfinance", exc)
            await self._preload_session_yfinance()

    async def _preload_session_yfinance(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            import yfinance as yf
            today = datetime.now(_ET).strftime("%Y-%m-%d")
            intraday = await loop.run_in_executor(
                None,
                lambda: yf.download(
                    self.symbols, period="1d", interval="1m",
                    progress=False, auto_adjust=False, threads=True,
                ),
            )
            loaded = 0
            for sym in self.symbols:
                try:
                    opens = intraday["Open"][sym].dropna()
                    vols = intraday["Volume"][sym].dropna()
                    if not opens.empty:
                        self._daily_open[sym] = float(opens.iloc[0])
                    if not vols.empty:
                        self._cumulative_vol[sym] = float(vols.sum())
                    if not opens.empty or not vols.empty:
                        loaded += 1
                except Exception:
                    pass
            self._session_date = today
            self.log.info("Session state preloaded for %d symbols (yfinance)", loaded)
        except Exception as exc:
            self.log.error("Session state preload failed: %s", exc)

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

        backoff = 5.0
        while True:
            stream = StockDataStream(api_key, secret)

            async def bar_handler(bar) -> None:  # type: ignore[no-untyped-def]
                await self._process_bar(bar.symbol, float(bar.close), float(bar.volume))

            stream.subscribe_bars(bar_handler, *self.symbols)
            self.log.info("Alpaca bar stream started (%d symbols)", len(self.symbols))

            t = threading.Thread(target=stream.run, daemon=True)
            t.start()
            start = asyncio.get_event_loop().time()
            try:
                while t.is_alive():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                stream.stop()
                raise

            # Thread died — connection dropped or limit exceeded
            alive_for = asyncio.get_event_loop().time() - start
            if alive_for < 10:
                # Failed almost immediately — likely connection limit; back off
                self.log.warning("Stream died after %.0fs — retrying in %.0fs", alive_for, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)  # cap at 2 min
            else:
                # Was alive for a while — reset backoff and reconnect quickly
                backoff = 5.0
                self.log.info("Stream disconnected after %.0fs — reconnecting", alive_for)
                await asyncio.sleep(2)

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

        trigger = "SIGNAL"
        self.log.info(
            "Signal: %s  RVOL=%.1fx  price=%+.1f%%  [%s]", sym, rvol, price_change_pct, trigger
        )
        await self.bus.publish("market.signal", {
            "symbol": sym,
            "price": price,
            "rvol": rvol,
            "price_change_pct": price_change_pct,
            "vol_direction": "UP" if price_change_pct > 0 else "DOWN",
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
