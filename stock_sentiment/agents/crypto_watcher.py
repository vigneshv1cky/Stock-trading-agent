"""CryptoWatcherAgent — polls Alpaca crypto bars every 60s and fires market.signal.

Runs 24/7 (no market hours gate).
Signal gate: RVOL ≥ 1.5× AND |24h price change| ≥ 2%
Debounce: 5-min per-symbol cooldown.
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from .base import BaseAgent
from .event_bus import EventBus

CRYPTO_UNIVERSE = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD",
    "LINK/USD", "DOGE/USD", "LTC/USD",
]

_RVOL_THRESHOLD = 1.5
_PRICE_MOVE_THRESHOLD = 2.0
_SIGNAL_COOLDOWN_S = 300
_POLL_INTERVAL_S = 60


class CryptoWatcherAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "CryptoWatcherAgent")
        self._avg_volumes: dict[str, float] = {}
        self._last_signal: dict[str, float] = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical import CryptoHistoricalDataClient
            api_key = os.environ.get("ALPACA_API_KEY", "")
            secret = os.environ.get("ALPACA_SECRET_KEY", "")
            self._client = CryptoHistoricalDataClient(api_key, secret)
        return self._client

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        self.log.info("CryptoWatcherAgent: loading avg volumes…")
        try:
            await loop.run_in_executor(None, self._load_volumes)
        except Exception as exc:
            self.log.warning("Volume preload failed: %s", exc)
        self.log.info(
            "CryptoWatcherAgent ready — watching %d symbols every %ds",
            len(CRYPTO_UNIVERSE), _POLL_INTERVAL_S,
        )
        while True:
            loop = asyncio.get_event_loop()
            try:
                signals = await loop.run_in_executor(None, self._fetch_signals)
                for sig in signals:
                    await self.bus.publish("market.signal", sig)
            except Exception as exc:
                self.log.warning("Scan error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL_S)

    @staticmethod
    def _bars_to_df(bars_list: list):
        import pandas as pd
        return pd.DataFrame([
            {"volume": b.volume, "close": b.close, "open": b.open,
             "high": b.high, "low": b.low}
            for b in bars_list
        ])

    def _load_volumes(self) -> None:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = self._get_client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=25)
        req = CryptoBarsRequest(
            symbol_or_symbols=CRYPTO_UNIVERSE,
            timeframe=TimeFrame.Hour,
            start=start,
            end=end,
        )
        bars = client.get_crypto_bars(req)
        for sym in CRYPTO_UNIVERSE:
            try:
                bars_list = bars[sym] if isinstance(bars[sym], list) else list(bars[sym])
                df = self._bars_to_df(bars_list)
                if len(df) >= 10:
                    self._avg_volumes[sym] = float(df["volume"].mean())
            except Exception as exc:
                self.log.warning("Volume load failed for %s: %s", sym, exc)
        self.log.info("CryptoWatcherAgent: volumes loaded for %d symbols", len(self._avg_volumes))

    def _fetch_signals(self) -> list:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = self._get_client()
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=26)
        req = CryptoBarsRequest(
            symbol_or_symbols=CRYPTO_UNIVERSE,
            timeframe=TimeFrame.Hour,
            start=start,
            end=now,
        )
        bars = client.get_crypto_bars(req)

        signals = []
        for sym in CRYPTO_UNIVERSE:
            try:
                bars_list = bars[sym] if isinstance(bars[sym], list) else list(bars[sym])
                df = self._bars_to_df(bars_list)
                if len(df) < 2:
                    continue

                last_vol = float(df.iloc[-1]["volume"])
                price = float(df.iloc[-1]["close"])
                price_24h = float(df.iloc[max(0, len(df) - 24)]["close"])

                avg_vol = self._avg_volumes.get(sym, float(df["volume"].mean()))
                rvol = last_vol / avg_vol if avg_vol > 0 else 1.0
                change_pct = (price - price_24h) / price_24h * 100 if price_24h else 0.0

                if rvol < _RVOL_THRESHOLD or abs(change_pct) < _PRICE_MOVE_THRESHOLD:
                    continue

                now_ts = time.time()
                if now_ts - self._last_signal.get(sym, 0) < _SIGNAL_COOLDOWN_S:
                    continue
                self._last_signal[sym] = now_ts

                signals.append({
                    "symbol": sym,
                    "price": price,
                    "rvol": round(rvol, 1),
                    "price_change_pct": round(change_pct, 2),
                    "trigger_type": "CRYPTO_SCAN",
                    "asset_class": "crypto",
                    "timestamp": now.isoformat(),
                })
                self.log.info(
                    "Crypto signal: %s  RVOL=%.1fx  change=%+.1f%%",
                    sym, rvol, change_pct,
                )
            except Exception as exc:
                self.log.debug("%s error: %s", sym, exc)

        return signals
