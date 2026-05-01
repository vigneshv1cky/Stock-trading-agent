"""CryptoWatcherAgent — streams 1-min bars from Alpaca Crypto WebSocket and fires market.signal events.

Signal gate: RVOL ≥ 1.5 AND |price change| ≥ 3%  (higher noise floor than equities)
Debounce:    30-min per-symbol cooldown (24/7 market needs longer cooldown)
Session:     resets at UTC midnight
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from .base import BaseAgent
from .event_bus import EventBus

_UTC = timezone.utc

_RVOL_THRESHOLD = 1
_PRICE_MOVE_THRESHOLD = 0.5  # % — crypto noise floor (vs 0.7% for stocks)
_SIGNAL_COOLDOWN_S = 1800  # 30 min — 24/7 market gets longer debounce
_MINUTES_PER_DAY = 1440.0


class CryptoWatcherAgent(BaseAgent):
    def __init__(self, bus: EventBus, symbols: list[str]):
        super().__init__(bus, "CryptoWatcherAgent")
        self.symbols = symbols
        self._avg_volumes: dict[str, float] = {}
        self._session_open: dict[str, float] = {}
        self._cumulative_vol: dict[str, float] = {}
        self._session_date: str = ""  # YYYY-MM-DD UTC
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
            self.log.warning("No Alpaca keys — CryptoWatcherAgent disabled")

    # ------------------------------------------------------------------
    # Volume pre-load (Alpaca Crypto Historical — no auth needed)
    # ------------------------------------------------------------------

    async def _preload_avg_volumes(self) -> None:
        self.log.info("Preloading 20-day avg volumes for %d crypto symbols…", len(self.symbols))
        loop = asyncio.get_running_loop()
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import CryptoBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame  # type: ignore[import-untyped]

            client = CryptoHistoricalDataClient()
            end = datetime.now(_UTC)
            start = end - timedelta(days=30)
            request = CryptoBarsRequest(symbol_or_symbols=self.symbols, timeframe=TimeFrame.Day, start=start, end=end)
            bars = await loop.run_in_executor(None, client.get_crypto_bars, request)
            for sym in self.symbols:
                try:
                    sym_bars = bars[sym]
                    if sym_bars and len(sym_bars) >= 5:
                        vols = [b.volume for b in sym_bars[-20:]]
                        self._avg_volumes[sym] = float(sum(vols) / len(vols))
                except Exception:
                    pass
            self.log.info("Crypto avg volumes loaded for %d/%d symbols", len(self._avg_volumes), len(self.symbols))
        except Exception as exc:
            self.log.warning("Crypto volume preload failed: %s", exc)

        await self._preload_session_state()

    async def _preload_session_state(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            from alpaca.data.historical import CryptoHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import CryptoBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore[import-untyped]

            now = datetime.now(_UTC)
            today_str = now.strftime("%Y-%m-%d")
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            client = CryptoHistoricalDataClient()
            request = CryptoBarsRequest(
                symbol_or_symbols=self.symbols, timeframe=TimeFrame(1, TimeFrameUnit.Minute), start=day_start, end=now
            )
            bars = await loop.run_in_executor(None, client.get_crypto_bars, request)
            loaded = 0
            for sym in self.symbols:
                try:
                    sym_bars = bars[sym]
                    if sym_bars:
                        self._session_open[sym] = float(sym_bars[0].open)
                        self._cumulative_vol[sym] = float(sum(b.volume for b in sym_bars))
                        loaded += 1
                except Exception:
                    pass
            self._session_date = today_str
            self.log.info("Crypto session state preloaded for %d symbols", loaded)
        except Exception as exc:
            self.log.warning("Crypto session preload failed: %s", exc)

    # ------------------------------------------------------------------
    # Session reset (UTC midnight)
    # ------------------------------------------------------------------

    def _reset_session_if_needed(self) -> None:
        today = datetime.now(_UTC).strftime("%Y-%m-%d")
        if today != self._session_date:
            self._session_date = today
            self._session_open.clear()
            self._cumulative_vol.clear()

    # ------------------------------------------------------------------
    # Alpaca Crypto WebSocket
    # ------------------------------------------------------------------

    async def _stream_bars(self, api_key: str, secret: str) -> None:
        import threading
        from alpaca.data.live import CryptoDataStream  # type: ignore[import-untyped]

        backoff = 5.0
        while True:
            stream = CryptoDataStream(api_key, secret)

            async def bar_handler(bar) -> None:  # type: ignore[no-untyped-def]
                await self._process_bar(bar.symbol, float(bar.close), float(bar.volume))

            stream.subscribe_bars(bar_handler, *self.symbols)
            self.log.info("Alpaca Crypto bar stream started (%d symbols)", len(self.symbols))

            t = threading.Thread(target=stream.run, daemon=True)
            t.start()
            start = asyncio.get_event_loop().time()
            try:
                while t.is_alive():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                stream.stop()
                raise

            alive_for = asyncio.get_event_loop().time() - start
            if alive_for < 10:
                self.log.warning("Crypto stream died after %.0fs — retrying in %.0fs", alive_for, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
            else:
                backoff = 5.0
                self.log.info("Crypto stream disconnected after %.0fs — reconnecting", alive_for)
                await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Bar processing
    # ------------------------------------------------------------------

    async def _process_bar(self, sym: str, price: float, volume: float) -> None:
        self._reset_session_if_needed()

        if sym not in self._session_open:
            self._session_open[sym] = price
        self._cumulative_vol[sym] = self._cumulative_vol.get(sym, 0.0) + volume

        avg_daily = self._avg_volumes.get(sym, 0.0)
        if avg_daily <= 0:
            return

        now_utc = datetime.now(_UTC)
        minutes_elapsed = max(1.0, now_utc.hour * 60.0 + now_utc.minute + now_utc.second / 60.0)
        expected_vol = avg_daily * (minutes_elapsed / _MINUTES_PER_DAY)
        rvol = self._cumulative_vol[sym] / expected_vol if expected_vol > 0 else 0.0

        open_price = self._session_open[sym]
        price_change_pct = ((price - open_price) / open_price * 100.0) if open_price > 0 else 0.0

        if rvol < _RVOL_THRESHOLD or abs(price_change_pct) < _PRICE_MOVE_THRESHOLD:
            return

        if time.time() - self._last_signal.get(sym, 0.0) < _SIGNAL_COOLDOWN_S:
            return
        self._last_signal[sym] = time.time()

        self.log.info("Crypto signal: %s  RVOL=%.1fx  price=%+.1f%%", sym, rvol, price_change_pct)
        await self.bus.publish(
            "market.signal",
            {
                "symbol": sym,
                "price": price,
                "rvol": rvol,
                "price_change_pct": price_change_pct,
                "trigger_type": "SIGNAL",
                "asset_class": "crypto",
                "timestamp": now_utc.isoformat(),
            },
        )
