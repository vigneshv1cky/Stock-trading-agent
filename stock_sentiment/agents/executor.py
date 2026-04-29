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
_TRADE_HISTORY = os.path.expanduser("~/.stock_screener/trade_history.json")
_HISTORY_MAX = 100
_STOP_AUDIT_INTERVAL_S = 300   # 5 min
_EOD_HOUR, _EOD_MINUTE = 15, 30


class ExecutorAgent(BaseAgent):
    def __init__(self, bus: EventBus, dry_run: bool = False, mode: str = "stocks"):
        super().__init__(bus, "ExecutorAgent")
        self._queue = bus.subscribe("trade.approved")
        self.dry_run = dry_run
        self.mode = mode  # "stocks", "crypto", "both"
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
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._startup_mode_cleanup)
        await loop.run_in_executor(None, self._startup_position_audit)
        await asyncio.gather(
            self._process_trades(),
            self._stop_audit_loop(),
            self._eod_check_loop(),
        )

    def _startup_mode_cleanup(self) -> None:
        """Close positions that don't belong in the current mode."""
        if self.mode == "both" or self.dry_run:
            return
        broker = self._get_broker()
        if not broker.client:
            return
        try:
            positions = broker.client.get_all_positions()
            held = self._load_held_cache()
            closed = []
            for pos in positions:
                sym = pos.symbol
                is_crypto = "/" in sym
                if self.mode == "stocks" and is_crypto:
                    broker._close_position_safely(sym)
                    held.pop(sym, None)
                    closed.append(sym)
                elif self.mode == "crypto" and not is_crypto:
                    broker._close_position_safely(sym)
                    held.pop(sym, None)
                    closed.append(sym)
            if closed:
                self._save_held_cache(held)
                self.log.info("Mode cleanup [%s]: closed %s", self.mode, ", ".join(closed))
        except Exception as exc:
            self.log.error("Mode cleanup error: %s", exc)

    # ------------------------------------------------------------------
    # Startup audit — evaluate inherited positions, close losers, set stops
    # ------------------------------------------------------------------

    def _startup_position_audit(self) -> None:
        if self.dry_run:
            return
        broker = self._get_broker()
        if not broker.client:
            return
        try:
            positions = broker.client.get_all_positions()

            # Cancel any open orders from previous session
            import time as _time
            try:
                open_orders = broker.client.get_orders()
                if open_orders:
                    broker.client.cancel_orders()
                    _time.sleep(3)
                    self.log.info("Startup audit: cancelled %d open orders", len(open_orders))
            except Exception as exc:
                self.log.warning("Could not cancel open orders: %s", exc)

            held = self._load_held_cache()

            # Remove stale held_cache entries that no longer exist in Alpaca
            live_symbols = {pos.symbol for pos in positions}
            stale = [sym for sym in list(held.keys()) if sym not in live_symbols]
            for sym in stale:
                held.pop(sym)
                self.log.info("Startup: removed stale cache entry %s", sym)

            if not positions:
                self._save_held_cache(held)
                return

            # Backfill held_cache for any live position not already tracked
            from datetime import datetime as _dt
            for pos in positions:
                if pos.symbol not in held:
                    is_long = pos.side.value == "long"
                    held[pos.symbol] = {
                        "type": "DAY",
                        "direction": "LONG" if is_long else "SHORT",
                        "entered_at": _dt.now(_ET).isoformat(),
                        "score": 50.0,
                    }

            closed, stopped = [], []

            for pos in positions:
                sym = pos.symbol
                pnl_pct = float(pos.unrealized_plpc) * 100
                is_long = pos.side.value == "long"

                # Loss > 0.5% on startup — cut it, don't wait for stop
                if pnl_pct < -0.5:
                    try:
                        broker._close_position_safely(sym)
                        held.pop(sym, None)
                        closed.append(f"{sym} ({pnl_pct:+.1f}%)")
                        self.log.info("Startup CLOSE %s: loss=%.1f%% exceeds stop threshold", sym, pnl_pct)
                    except Exception as exc:
                        self.log.error("Startup close failed %s: %s", sym, exc)
                    continue

                # Still viable — tighter stop as profit grows
                if pnl_pct >= 20.0:
                    trail = 0.5   # almost all gains locked in
                elif pnl_pct >= 10.0:
                    trail = 0.8   # tight on solid winner
                elif pnl_pct >= 5.0:
                    trail = 1.5   # some room on moderate winner
                elif pnl_pct >= 0.0:
                    trail = 0.8 if not is_long else 1.5  # near-flat: tight as new entry
                else:
                    trail = 0.5   # loss 0–0.5%: exit fast if it worsens at all

                try:
                    from alpaca.trading.requests import TrailingStopOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    close_side = OrderSide.SELL if is_long else OrderSide.BUY
                    req = TrailingStopOrderRequest(
                        symbol=sym,
                        qty=abs(float(pos.qty)),
                        side=close_side,
                        time_in_force=TimeInForce.DAY,
                        trail_percent=trail,
                    )
                    broker.client.submit_order(req)
                    stopped.append(f"{sym} trail={trail}% ({pnl_pct:+.1f}%)")
                except Exception as exc:
                    self.log.error("Startup stop failed %s: %s", sym, exc)

            self._save_held_cache(held)
            if closed:
                self.log.info("Startup audit CLOSED: %s", ", ".join(closed))
            if stopped:
                self.log.info("Startup audit STOPS SET: %s", ", ".join(stopped))

        except Exception as exc:
            self.log.error("Startup audit error: %s", exc)

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
                is_crypto = "/" in sym
                buy_fn = broker._place_crypto_buy if is_crypto else broker._place_market_buy
                price, qty = await loop.run_in_executor(
                    None, buy_fn,
                    sym, pred.current_price, portfolio_value,
                )
                if qty:
                    held[sym] = {
                        "type": "CRYPTO" if is_crypto else "DAY",
                        "direction": "LONG",
                        "entered_at": datetime.now(_ET).isoformat(),
                        "score": pred.overall_score,
                    }
                    self._save_held_cache(held)
                    self._exec_log["bought"].append({"symbol": sym, "price": price, "qty": qty})
                    self._flush_log()
                    self._append_history({"action": "BUY", "symbol": sym, "price": price, "qty": qty})
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
                        "score": pred.overall_score,
                    }
                    self._save_held_cache(held)
                    self._exec_log["shorted"].append({"symbol": sym, "price": price, "qty": qty})
                    self._flush_log()
                    self._append_history({"action": "SHORT", "symbol": sym, "price": price, "qty": qty})
                    await self.bus.publish("trade.executed", {
                        "symbol": sym, "action": "SHORT", "direction": "SHORT",
                        "price": price, "qty": qty,
                        "prediction_score": pred.overall_score,
                        "archetype": pred.archetype if pred else "",
                    })
                    self.log.info("SHORT %s  qty=%d  @$%.2f", sym, qty, price)

            elif action == "CLOSE":
                close_price, close_qty, entry_price, direction = 0.0, 0, 0.0, "LONG"
                pnl, pnl_pct = 0.0, 0.0
                try:
                    positions = await loop.run_in_executor(None, broker.client.get_all_positions)
                    for p in positions:
                        if p.symbol == sym:
                            close_price = float(p.current_price)
                            close_qty = abs(int(float(p.qty)))
                            entry_price = float(p.avg_entry_price)
                            direction = "SHORT" if float(p.qty) < 0 else "LONG"
                            pnl = float(p.unrealized_pl)
                            pnl_pct = float(p.unrealized_plpc) * 100
                            break
                except Exception:
                    pass
                await loop.run_in_executor(None, broker._close_position_safely, sym)
                held.pop(sym, None)
                self._save_held_cache(held)
                reason = data.get("reason", "")
                self._exec_log["sold"].append({
                    "symbol": sym, "reason": reason,
                    "price": close_price or None, "qty": close_qty or None,
                    "entry_price": entry_price or None,
                    "pnl": round(pnl, 2) if pnl else None,
                    "pnl_pct": round(pnl_pct, 2) if pnl_pct else None,
                    "direction": direction,
                })
                self._flush_log()
                self._append_history({
                    "action": "CLOSE", "symbol": sym,
                    "price": close_price or None, "qty": close_qty or None,
                    "pnl": round(pnl, 2) if pnl else None,
                    "pnl_pct": round(pnl_pct, 2) if pnl_pct else None,
                })
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
                # Verify every position still has an open stop — re-set if missing
                # (Alpaca cancels DAY stops at 4 PM; this catches any gaps during the session)
                positions = await loop.run_in_executor(None, broker.client.get_all_positions)
                open_orders = await loop.run_in_executor(None, broker.client.get_orders)
                stopped_syms = {o.symbol for o in open_orders}
                for pos in positions:
                    if pos.symbol in stopped_syms:
                        continue
                    pnl_pct = float(pos.unrealized_plpc) * 100
                    is_long = pos.side.value == "long"
                    trail = 3.0 if pnl_pct >= 0 else 1.5
                    from alpaca.trading.requests import TrailingStopOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    req = TrailingStopOrderRequest(
                        symbol=pos.symbol,
                        qty=abs(float(pos.qty)),
                        side=OrderSide.SELL if is_long else OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                        trail_percent=trail,
                    )
                    await loop.run_in_executor(None, broker.client.submit_order, req)
                    self.log.info("Re-set missing stop: %s trail=%.1f%% (pnl=%.1f%%)",
                                  pos.symbol, trail, pnl_pct)
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
                    if "/" in pos.symbol:  # skip crypto — runs 24/7
                        continue
                    try:
                        close_price = float(pos.current_price)
                        close_qty = abs(int(float(pos.qty)))
                        pnl = float(pos.unrealized_pl)
                        pnl_pct = float(pos.unrealized_plpc) * 100
                        await loop.run_in_executor(
                            None, broker._close_position_safely, pos.symbol
                        )
                        self._append_history({
                            "action": "CLOSE", "symbol": pos.symbol,
                            "price": close_price, "qty": close_qty,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                        })
                        held = self._load_held_cache()
                        held.pop(pos.symbol, None)
                        self._save_held_cache(held)
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

    def _append_history(self, entry: dict) -> None:
        try:
            os.makedirs(os.path.dirname(_TRADE_HISTORY), exist_ok=True)
            history: list = []
            if os.path.exists(_TRADE_HISTORY):
                with open(_TRADE_HISTORY) as fh:
                    history = json.load(fh)
            entry["timestamp"] = datetime.now(_ET).isoformat()
            history.append(entry)
            history = history[-_HISTORY_MAX:]
            with open(_TRADE_HISTORY, "w") as fh:
                json.dump(history, fh, indent=2)
        except Exception:
            pass
