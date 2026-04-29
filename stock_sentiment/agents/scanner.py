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

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        # Pre-load 20-day avg volumes (reuse WatcherAgent's data if available,
        # otherwise fetch independently)
        await loop.run_in_executor(None, self._load_avg_volumes)
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
        import yfinance as yf
        from .macro import MacroAgent

        macro  = MacroAgent.current
        regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"

        # Tighten thresholds in stressed regimes
        rvol_min = _RVOL_MIN * (1.2 if regime == "RISK_OFF" else 1.5 if regime == "PANIC" else 1.0)
        move_min = _MOVE_MIN_PCT * (1.2 if regime == "RISK_OFF" else 1.5 if regime == "PANIC" else 1.0)

        # Load held symbols to skip re-entry
        held_syms = self._load_held_syms()

        try:
            data = yf.download(
                self._symbols,
                period="1d",
                interval="1m",
                progress=False,
                auto_adjust=True,
                threads=True,
            )
        except Exception as exc:
            self.log.warning("Scanner yfinance download failed: %s", exc)
            return 0

        if data is None or data.empty:
            return 0

        fired = 0
        now_ts = time.time()
        now_et = datetime.now(_ET)

        for sym in self._symbols:
            try:
                # Skip held symbols and recently signalled ones
                if sym in held_syms:
                    continue
                if now_ts - self._last_signal.get(sym, 0) < _DEBOUNCE_S:
                    continue

                closes  = data["Close"][sym].dropna()
                volumes = data["Volume"][sym].dropna()

                if len(closes) < 5:
                    continue

                open_price    = float(closes.iloc[0])
                current_price = float(closes.iloc[-1])
                if open_price <= 0:
                    continue

                price_change_pct = (current_price - open_price) / open_price * 100
                if abs(price_change_pct) < move_min:
                    continue

                # RVOL: today's cumulative volume vs 20-day avg
                avg_vol = self._avg_volumes.get(sym, 0)
                if avg_vol <= 0:
                    continue

                cumulative_vol = float(volumes.sum())
                minutes_elapsed = max(1, (now_et.hour - 9) * 60 + (now_et.minute - 30))
                # Scale avg to elapsed minutes so RVOL is apples-to-apples
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_avg_volumes(self) -> None:
        import yfinance as yf
        self.log.info("ScannerAgent: loading 20-day avg volumes…")
        try:
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
            self.log.info("ScannerAgent: avg volumes loaded for %d symbols",
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
