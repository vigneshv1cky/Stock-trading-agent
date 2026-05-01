"""ScannerAgent — proactive periodic scan of the full universe every 15 min.

Complements WatcherAgent (reactive WebSocket) with a scheduled batch scan
so signals fire even on low-volatility days when nothing crosses the RVOL gate.

Publishes to: market.signal  (same topic as WatcherAgent — reuses full pipeline)

Scan logic (looser than WatcherAgent to surface quieter setups):
  • RVOL ≥ 1.2×  (vs WatcherAgent's 1.5×)
  • |price change from open| ≥ 1.0%  (vs 2.0%)
  • Not already held, not in cooldown
  • Only during 9:45 AM – 3:30 PM ET
  • 15-min per-symbol debounce (avoids flooding pipeline every scan)
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

_SCAN_INTERVAL_S  = 900      # scan every 15 min
_RVOL_MIN         = 1.2      # looser than WatcherAgent (1.5)
_MOVE_MIN_PCT     = 1.0      # looser than WatcherAgent (2.0%)
_DEBOUNCE_S       = 900      # don't re-signal same symbol within 15 min
_OPEN_GATE_MIN    = 15       # skip first 15 min (9:30–9:45)
_CLOSE_GATE_MIN   = 30       # skip last 30 min before 4 PM


class ScannerAgent(BaseAgent):
    def __init__(self, bus: EventBus, symbols: list[str]):
        super().__init__(bus, "ScannerAgent")
        self._symbols = symbols
        self._last_signal: dict[str, float] = {}
        self._avg_volumes: dict[str, float] = {}
        self._api_key = os.environ.get("ALPACA_API_KEY", "")
        self._secret = os.environ.get("ALPACA_SECRET_KEY", "")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_avg_volumes, self._api_key, self._secret)
        self.log.info("ScannerAgent ready — scanning %d symbols every %d min",
                      len(self._symbols), _SCAN_INTERVAL_S // 60)

        # Stagger first scan by 5 min so it doesn't overlap WatcherAgent startup
        await asyncio.sleep(300)

        while True:
            now = datetime.now(_ET)
            minutes_since_open = (now.hour - 9) * 60 + (now.minute - 30)
            minutes_to_close   = (16 * 60) - (now.hour * 60 + now.minute)

            if _OPEN_GATE_MIN <= minutes_since_open and minutes_to_close > _CLOSE_GATE_MIN:
                fired = await loop.run_in_executor(None, self._scan, loop)
                if fired:
                    self.log.info("Scan complete — %d signals fired", fired)
                else:
                    self.log.debug("Scan complete — no new signals")
            else:
                self.log.debug("Scanner outside trading window — skipping")

            await asyncio.sleep(_SCAN_INTERVAL_S)

    # ------------------------------------------------------------------
    # Batch scan — runs in executor
    # ------------------------------------------------------------------

    def _scan(self, loop: asyncio.AbstractEventLoop) -> int:
        from .macro import MacroAgent

        macro  = MacroAgent.current
        regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"

        rvol_min = _RVOL_MIN * (1.2 if regime == "RISK_OFF" else 1.5 if regime == "PANIC" else 1.0)
        move_min = _MOVE_MIN_PCT * (1.2 if regime == "RISK_OFF" else 1.5 if regime == "PANIC" else 1.0)

        held_syms = self._load_held_syms()

        # Build per-symbol {sym: (open_price, current_price, cumulative_vol)}
        bars_by_sym: dict[str, tuple[float, float, float]] = {}

        if self._api_key and self._secret:
            bars_by_sym = self._fetch_intraday_alpaca()
        else:
            bars_by_sym = self._fetch_intraday_yfinance()

        if not bars_by_sym:
            return 0

        fired = 0
        now_ts = time.time()
        now_et = datetime.now(_ET)
        minutes_elapsed = max(1, (now_et.hour - 9) * 60 + (now_et.minute - 30))

        for sym in self._symbols:
            try:
                if sym in held_syms:
                    continue
                if now_ts - self._last_signal.get(sym, 0) < _DEBOUNCE_S:
                    continue
                if sym not in bars_by_sym:
                    continue

                open_price, current_price, cumulative_vol = bars_by_sym[sym]
                if open_price <= 0:
                    continue

                price_change_pct = (current_price - open_price) / open_price * 100
                if abs(price_change_pct) < move_min:
                    continue

                avg_vol = self._avg_volumes.get(sym, 0)
                if avg_vol <= 0:
                    continue

                expected_vol = avg_vol * (minutes_elapsed / 390.0)
                rvol = cumulative_vol / expected_vol if expected_vol > 0 else 0.0
                if rvol < rvol_min:
                    continue

                self._last_signal[sym] = now_ts
                asyncio.run_coroutine_threadsafe(
                    self.bus.publish("market.signal", {
                        "symbol":           sym,
                        "price":            current_price,
                        "rvol":             round(rvol, 2),
                        "price_change_pct": round(price_change_pct, 2),
                        "trigger_type":     "SCAN",
                        "timestamp":        now_et.isoformat(),
                    }),
                    loop,
                )
                self.log.info("Scan signal: %s  RVOL=%.1fx  change=%+.1f%%  regime=%s",
                              sym, rvol, price_change_pct, regime)
                fired += 1

            except Exception:
                continue

        return fired

    def _fetch_intraday_alpaca(self) -> dict[str, tuple[float, float, float]]:
        try:
            from alpaca.data.enums import DataFeed  # type: ignore[import-untyped]
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import StockBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # type: ignore[import-untyped]

            client = StockHistoricalDataClient(self._api_key, self._secret)
            now = datetime.now(_ET)
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            request = StockBarsRequest(
                symbol_or_symbols=self._symbols,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=market_open,
                end=now,
                feed=DataFeed.IEX,
            )
            bars = client.get_stock_bars(request)
            result: dict[str, tuple[float, float, float]] = {}
            for sym in self._symbols:
                try:
                    sym_bars = bars[sym]
                    if sym_bars and len(sym_bars) >= 5:
                        result[sym] = (
                            float(sym_bars[0].open),
                            float(sym_bars[-1].close),
                            float(sum(b.volume for b in sym_bars)),
                        )
                except Exception:
                    pass
            return result
        except Exception as exc:
            self.log.warning("Scanner Alpaca intraday fetch failed: %s — falling back to yfinance", exc)
            return self._fetch_intraday_yfinance()

    def _fetch_intraday_yfinance(self) -> dict[str, tuple[float, float, float]]:
        try:
            import yfinance as yf
            data = yf.download(
                self._symbols, period="1d", interval="1m",
                progress=False, auto_adjust=True, threads=True,
            )
            if data is None or data.empty:
                return {}
            result: dict[str, tuple[float, float, float]] = {}
            for sym in self._symbols:
                try:
                    closes  = data["Close"][sym].dropna()
                    volumes = data["Volume"][sym].dropna()
                    if len(closes) >= 5:
                        result[sym] = (
                            float(closes.iloc[0]),
                            float(closes.iloc[-1]),
                            float(volumes.sum()),
                        )
                except Exception:
                    pass
            return result
        except Exception as exc:
            self.log.warning("Scanner yfinance intraday fetch failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_avg_volumes(self, api_key: str = "", secret: str = "") -> None:
        self.log.info("ScannerAgent: loading 20-day avg volumes…")
        if api_key and secret:
            self._load_volumes_alpaca(api_key, secret)
        else:
            self._load_volumes_yfinance()

    def _load_volumes_alpaca(self, api_key: str, secret: str) -> None:
        try:
            from alpaca.data.enums import DataFeed  # type: ignore[import-untyped]
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore[import-untyped]
            from alpaca.data.requests import StockBarsRequest  # type: ignore[import-untyped]
            from alpaca.data.timeframe import TimeFrame  # type: ignore[import-untyped]

            client = StockHistoricalDataClient(api_key, secret)
            end = datetime.now(_ET)
            start = end - timedelta(days=30)
            request = StockBarsRequest(
                symbol_or_symbols=self._symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
            )
            bars = client.get_stock_bars(request)
            for sym in self._symbols:
                try:
                    sym_bars = bars[sym]
                    if sym_bars and len(sym_bars) >= 5:
                        vols = [b.volume for b in sym_bars[-20:]]
                        self._avg_volumes[sym] = float(sum(vols) / len(vols))
                except Exception:
                    pass
            self.log.info("ScannerAgent: avg volumes loaded for %d symbols (Alpaca)",
                          len(self._avg_volumes))
        except Exception as exc:
            self.log.warning("ScannerAgent Alpaca volume preload failed: %s — falling back to yfinance", exc)
            self._load_volumes_yfinance()

    def _load_volumes_yfinance(self) -> None:
        try:
            import yfinance as yf
            data = yf.download(
                self._symbols, period="1mo", interval="1d",
                progress=False, auto_adjust=True, threads=True,
            )
            for sym in self._symbols:
                try:
                    vols = data["Volume"][sym].dropna().tail(20)
                    if len(vols) >= 5:
                        self._avg_volumes[sym] = float(vols.mean())
                except Exception:
                    pass
            self.log.info("ScannerAgent: avg volumes loaded for %d symbols (yfinance)",
                          len(self._avg_volumes))
        except Exception as exc:
            self.log.warning("ScannerAgent volume preload failed: %s", exc)

    @staticmethod
    def _load_held_syms() -> set:
        import json
        import os
        path = os.path.expanduser("~/.stock_screener/held_cache.json")
        try:
            if os.path.exists(path):
                with open(path) as fh:
                    return set(json.load(fh).keys())
        except Exception:
            pass
        return set()
