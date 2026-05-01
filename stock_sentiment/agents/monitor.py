"""MonitorAgent — polls open Alpaca positions every 30 s and raises alerts.

Alert types:
  EARNINGS_REPORTED — earnings results just landed (actual EPS available within 48 h)
                      → RiskAgent calls Haiku to decide HOLD or CLOSE
  REEVAL            — position not re-evaluated in 30+ min → re-triggers the screener pipeline

Subscribes to: trade.executed  (to reset the re-eval timer per symbol)
Publishes to:  position.alert
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus

_POLL_INTERVAL_S = 30
_REEVAL_THRESHOLD_MIN = 30


class MonitorAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "MonitorAgent")
        self._trade_queue = bus.subscribe("trade.executed")
        self._last_eval: dict[str, datetime] = {}
        self._analyzed_earnings: dict[str, str] = {}  # "{sym}_{date}" → date_str
        self._client = None

    def _get_client(self):
        if self._client is None:
            from stock_sentiment.agents.broker import PaperBroker
            broker = PaperBroker()
            self._client = broker.client
        return self._client

    async def run(self) -> None:
        await asyncio.gather(
            self._poll_positions(),
            self._drain_trade_events(),
        )

    # ------------------------------------------------------------------
    # Track freshly executed trades so we don't immediately re-evaluate them
    # ------------------------------------------------------------------

    async def _drain_trade_events(self) -> None:
        while True:
            msg = await self._trade_queue.get()
            sym = msg["data"].get("symbol")
            if sym:
                self._last_eval[sym] = datetime.now(_ET)

    # ------------------------------------------------------------------
    # Position health poll
    # ------------------------------------------------------------------

    async def _poll_positions(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_POLL_INTERVAL_S)
            client = self._get_client()
            if not client:
                continue
            try:
                positions = await loop.run_in_executor(None, client.get_all_positions)
                now = datetime.now(_ET)
                for pos in positions:
                    sym = pos.symbol
                    alert = await loop.run_in_executor(None, self._check_position, sym, now)
                    if alert:
                        await self.bus.publish("position.alert", alert)
            except Exception as exc:
                self.log.error("Position poll error: %s", exc)

    def _check_position(self, sym: str, now: datetime) -> Optional[dict]:
        # Earnings result detection — stocks only (crypto has no earnings)
        if "/" not in sym:
            result = self._check_earnings_result(sym, now)
            if result:
                return result

        # Re-evaluation check
        last = self._last_eval.get(sym)
        if last is None or (now - last).total_seconds() > _REEVAL_THRESHOLD_MIN * 60:
            self._last_eval[sym] = now
            self.log.debug("Re-eval trigger: %s", sym)
            return {
                "symbol": sym,
                "alert_type": "REEVAL",
                "detail": f"Not re-evaluated in {_REEVAL_THRESHOLD_MIN}+ min",
            }
        return None

    def _check_earnings_result(self, sym: str, now: datetime) -> Optional[dict]:
        """Return EARNINGS_REPORTED alert if actual EPS landed in the last 48 h and
        we haven't already processed this report. Returns None otherwise."""
        import math
        try:
            import yfinance as yf
            df = yf.Ticker(sym).earnings_dates
            if df is None or df.empty:
                return None

            today = now.date()
            cutoff = today - timedelta(days=2)

            for dt_idx, row in df.iterrows():
                try:
                    row_date = dt_idx.date() if hasattr(dt_idx, "date") else dt_idx
                except Exception:
                    continue
                if row_date < cutoff:
                    break  # DataFrame is newest-first; nothing older is relevant

                reported = row.get("Reported EPS")
                if reported is None or (isinstance(reported, float) and math.isnan(reported)):
                    continue

                date_key = f"{sym}_{row_date}"
                if date_key in self._analyzed_earnings:
                    continue  # Already fired this report

                estimate = row.get("EPS Estimate")
                surprise = row.get("Surprise(%)", 0.0)
                if isinstance(surprise, float) and math.isnan(surprise):
                    surprise = 0.0
                    if estimate and not math.isnan(float(estimate)) and float(estimate) != 0:
                        surprise = (float(reported) - float(estimate)) / abs(float(estimate)) * 100

                self._analyzed_earnings[date_key] = row_date.isoformat()
                self.log.info(
                    "Earnings reported: %s  EPS=%.2f (est %.2f, %+.1f%%)",
                    sym, float(reported),
                    float(estimate) if estimate and not math.isnan(float(estimate)) else 0.0,
                    float(surprise),
                )
                return {
                    "symbol": sym,
                    "alert_type": "EARNINGS_REPORTED",
                    "earnings_date": row_date.isoformat(),
                    "reported_eps": float(reported),
                    "estimated_eps": float(estimate) if estimate and not math.isnan(float(estimate)) else None,
                    "surprise_pct": round(float(surprise), 1),
                    "detail": f"Earnings reported {row_date}",
                }
        except Exception:
            pass
        return None
