"""MonitorAgent — polls open Alpaca positions every 30 s and raises alerts.

Alert types:
  EARNINGS — earnings ≤ 3 days away → RiskAgent will close the position
  REEVAL   — position not re-evaluated in 30+ min → re-triggers the screener pipeline

Subscribes to: trade.executed  (to reset the re-eval timer per symbol)
Publishes to:  position.alert
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

from .base import BaseAgent
from .event_bus import EventBus

_POLL_INTERVAL_S = 30
_REEVAL_THRESHOLD_MIN = 30


class MonitorAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "MonitorAgent")
        self._trade_queue = bus.subscribe("trade.executed")
        self._last_eval: dict[str, datetime] = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            from stock_sentiment.market.broker import PaperBroker
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
        loop = asyncio.get_event_loop()
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
        import yfinance as yf

        # Earnings proximity
        try:
            ticker = yf.Ticker(sym)
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
            if dates:
                today = now.date()
                future = [
                    (dt.date() if hasattr(dt, "date") else dt)
                    for dt in dates
                    if (dt.date() if hasattr(dt, "date") else dt) >= today
                ]
                if future and (future[0] - today).days <= 3:
                    days_away = (future[0] - today).days
                    self.log.info("Earnings alert: %s in %dd", sym, days_away)
                    return {
                        "symbol": sym,
                        "alert_type": "EARNINGS",
                        "detail": f"Earnings in {days_away}d",
                    }
        except Exception:
            pass

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
