"""ExecutorAgent — places Alpaca orders and manages stops/EOD closes.

Subscribes to: trade.approved
Publishes to:  trade.executed, trade.closed

Reuses PaperBroker's battle-tested order/stop methods directly.
Stop audit runs every 5 min; EOD close checks every 60 s after 3:45 PM ET.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus

_HELD_CACHE = os.path.expanduser("~/.stock_screener/held_cache.json")
_EXEC_LOG = os.path.expanduser("~/.stock_screener/last_execution.json")
_STOP_AUDIT_INTERVAL_S = 300   # 5 min
_EOD_HOUR, _EOD_MINUTE = 15, 45


class ExecutorAgent(BaseAgent):
    def __init__(self, bus: EventBus, dry_run: bool = False):
        super().__init__(bus, "ExecutorAgent")
        self._queue = bus.subscribe("trade.approved")
        self.dry_run = dry_run
        self._broker = None
        self._exec_log: dict = {
            "timestamp": "", "trigger": "AGENT",
            "bought": [], "sold": [], "shorted": [], "covered": [], "swapped": [],
        }

    def _get_broker(self):
        if self._broker is None:
            from stock_sentiment.market.broker import PaperBroker
            self._broker = PaperBroker()
        return self._broker

    # ------------------------------------------------------------------
    # Entry point — three concurrent loops
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(
            self._process_trades(),
            self._stop_audit_loop(),
            self._eod_check_loop(),
        )

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    async def _process_trades(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._execute(msg["data"], loop))

    async def _execute(self, data: dict, loop: asyncio.AbstractEventLoop) -> None:
        sym = data["symbol"]
        action = data["action"]
        pred = data["prediction"]
        portfolio_value = data.get("portfolio_value", 0.0)

        if self.dry_run:
            self.log.info("DRY RUN: %s %s", action, sym)
            return

        broker = self._get_broker()
        if not broker.client:
            self.log.warning("Broker client unavailable — skipping %s %s", action, sym)
            return

        try:
            held = self._load_held_cache()

            if action == "BUY":
                price, qty = await loop.run_in_executor(
                    None, broker._place_market_buy,
                    sym, pred.current_price, portfolio_value,
                )
                if qty:
                    held[sym] = {
                        "type": "DAY", "direction": "LONG",
                        "entered_at": datetime.now(_ET).isoformat(),
                    }
                    self._save_held_cache(held)
                    self._exec_log["bought"].append({"symbol": sym, "price": price, "qty": qty})
                    self._flush_log()
                    await self.bus.publish("trade.executed", {
                        "symbol": sym, "action": "BUY", "direction": "LONG",
                        "price": price, "qty": qty,
                        "prediction_score": pred.overall_score,
                        "archetype": pred.archetype,
                    })
                    self.log.info("BUY %s  qty=%d  @$%.2f", sym, qty, price)

            elif action == "SHORT":
                price, qty = await loop.run_in_executor(
                    None, broker._place_market_short,
                    sym, pred.current_price, portfolio_value,
                )
                if qty:
                    held[sym] = {
                        "type": "DAY", "direction": "SHORT",
                        "entered_at": datetime.now(_ET).isoformat(),
                    }
                    self._save_held_cache(held)
                    self._exec_log["shorted"].append({"symbol": sym, "price": price, "qty": qty})
                    self._flush_log()
                    await self.bus.publish("trade.executed", {
                        "symbol": sym, "action": "SHORT", "direction": "SHORT",
                        "price": price, "qty": qty,
                        "prediction_score": pred.overall_score,
                        "archetype": pred.archetype if pred else "",
                    })
                    self.log.info("SHORT %s  qty=%d  @$%.2f", sym, qty, price)

            elif action == "CLOSE":
                await loop.run_in_executor(None, broker._close_position_safely, sym)
                held.pop(sym, None)
                self._save_held_cache(held)
                reason = data.get("reason", "")
                self._exec_log["sold"].append({"symbol": sym, "reason": reason})
                self._flush_log()
                await self.bus.publish("trade.closed", {
                    "symbol": sym, "action": "CLOSE", "reason": reason,
                    "prediction_score": pred.overall_score if pred else 0.0,
                    "archetype": pred.archetype if pred else "",
                    "critic_verdict": "",
                })
                self.log.info("CLOSE %s — %s", sym, reason)

        except Exception as exc:
            self.log.error("Execution error %s %s: %s", action, sym, exc)

    # ------------------------------------------------------------------
    # Periodic stop audit
    # ------------------------------------------------------------------

    async def _stop_audit_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(_STOP_AUDIT_INTERVAL_S)
            broker = self._get_broker()
            if not broker.client:
                continue
            try:
                positions = await loop.run_in_executor(None, broker.client.get_all_positions)
                held = self._load_held_cache()
                await loop.run_in_executor(None, broker._ensure_hard_stops, positions, held)
            except Exception as exc:
                self.log.error("Stop audit error: %s", exc)

    # ------------------------------------------------------------------
    # EOD position close (3:45 PM ET)
    # ------------------------------------------------------------------

    async def _eod_check_loop(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(60)
            now = datetime.now(_ET)
            eod_passed = now.hour > _EOD_HOUR or (
                now.hour == _EOD_HOUR and now.minute >= _EOD_MINUTE
            )
            if not eod_passed:
                continue
            broker = self._get_broker()
            if not broker.client:
                continue
            try:
                positions = await loop.run_in_executor(None, broker.client.get_all_positions)
                if not positions:
                    await asyncio.sleep(1800)
                    continue
                self.log.info("EOD close: %d positions", len(positions))
                for pos in positions:
                    try:
                        await loop.run_in_executor(
                            None, broker._close_position_safely, pos.symbol
                        )
                        await self.bus.publish("trade.closed", {
                            "symbol": pos.symbol, "action": "CLOSE", "reason": "EOD",
                            "prediction_score": 0.0, "archetype": "", "critic_verdict": "",
                        })
                    except Exception as exc:
                        self.log.error("EOD close failed %s: %s", pos.symbol, exc)
                await asyncio.sleep(1800)  # sleep past market close to avoid re-trigger
            except Exception as exc:
                self.log.error("EOD check error: %s", exc)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _load_held_cache(self) -> dict:
        try:
            if os.path.exists(_HELD_CACHE):
                with open(_HELD_CACHE) as fh:
                    return json.load(fh)
        except Exception:
            pass
        return {}

    def _save_held_cache(self, cache: dict) -> None:
        os.makedirs(os.path.dirname(_HELD_CACHE), exist_ok=True)
        with open(_HELD_CACHE, "w") as fh:
            json.dump(cache, fh, indent=2)

    def _flush_log(self) -> None:
        try:
            self._exec_log["timestamp"] = datetime.now(_ET).isoformat()
            os.makedirs(os.path.dirname(_EXEC_LOG), exist_ok=True)
            with open(_EXEC_LOG, "w") as fh:
                json.dump(self._exec_log, fh, indent=2)
        except Exception:
            pass
