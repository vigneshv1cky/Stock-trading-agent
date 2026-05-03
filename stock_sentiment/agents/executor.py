"""ExecutorAgent — places Alpaca orders and manages stops/EOD closes.

Subscribes to: trade.approved
Publishes to:  trade.executed, trade.closed

Reuses PaperBroker's battle-tested order/stop methods directly.
Stop audit runs every 5 min; EOD close checks every 60 s after 3:30 PM ET.
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

from stock_sentiment.agents.broker import _is_long_position

from .base import BaseAgent
from .event_bus import EventBus

_HELD_CACHE = os.path.expanduser("~/.stock_screener/held_cache.json")
_EXEC_LOG = os.path.expanduser("~/.stock_screener/last_execution.json")
_TRADE_HISTORY = os.path.expanduser("~/.stock_screener/trade_history.json")
_HISTORY_MAX = 100
_STOP_AUDIT_INTERVAL_S = 300   # 5 min
_EOD_HOUR, _EOD_MINUTE = 15, 30


class ExecutorAgent(BaseAgent):
    def __init__(self, bus: EventBus, dry_run: bool = False):
        super().__init__(bus, "ExecutorAgent")
        self._queue = bus.subscribe("trade.approved")
        self.dry_run = dry_run
        self._broker = None
        self._held_cache_lock = asyncio.Lock()
        self._exec_log: dict = {
            "timestamp": "", "trigger": "AGENT",
            "bought": [], "sold": [], "shorted": [], "covered": [], "swapped": [],
        }

    def _get_broker(self):
        if self._broker is None:
            from stock_sentiment.agents.broker import PaperBroker
            self._broker = PaperBroker()
        return self._broker

    # ------------------------------------------------------------------
    # Entry point — three concurrent loops
    # ------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._startup_position_audit)
        await asyncio.gather(
            self._process_trades(),
            self._stop_audit_loop(),
            self._eod_check_loop(),
        )

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

            # Backfill held_cache for any live position not already tracked.
            # Score=0 marks unknown-conviction positions so the LLM can displace them
            # when a scored signal arrives (score=50 made every legacy position look strong).
            from datetime import datetime as _dt
            for pos in positions:
                if pos.symbol not in held:
                    is_long = _is_long_position(pos)
                    held[pos.symbol] = {
                        "type": "DAY",
                        "direction": "LONG" if is_long else "SHORT",
                        "entered_at": _dt.now(_ET).isoformat(),
                        "score": 0.0,
                    }

            closed: list[str] = []
            stopped: list[str] = []

            for pos in positions:
                sym = pos.symbol
                pnl_pct = float(pos.unrealized_plpc) * 100
                is_long = _is_long_position(pos)

                qty = abs(float(pos.qty))
                if not qty.is_integer():
                    continue  # no stop for fractional positions

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
                        qty=int(qty),
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
        loop = asyncio.get_running_loop()
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
            if action == "BUY":
                self.log.info("Executor: submitting BUY %s (score=%.1f)", sym, pred.overall_score)
                price, qty = await loop.run_in_executor(
                    None, lambda: broker._place_market_buy(
                        sym, pred.current_price, portfolio_value,
                        pred.avg_sentiment, pred.bullish_count,
                    )
                )
                if not qty:
                    self.log.error("BUY %s FAILED — broker returned qty=0 (Alpaca rejected or price issue)", sym)
                if qty:
                    async with self._held_cache_lock:
                        held = self._load_held_cache()
                        held[sym] = {
                            "type": "DAY",
                            "direction": "LONG",
                            "entered_at": datetime.now(_ET).isoformat(),
                            "score": pred.overall_score,
                            "rvol": getattr(pred, "volume_ratio", None),
                            "rsi": getattr(pred, "rsi", None),
                            "avg_sentiment": getattr(pred, "avg_sentiment", None),
                            "change_today_pct": getattr(pred, "change_today_pct", None),
                            "change_1w_pct": getattr(pred, "change_1w_pct", None),
                            "critic_verdict": data.get("critic_verdict", ""),
                            "approval_reason": data.get("reason", ""),
                            "top_headlines": [h[0][:100] for h in (getattr(pred, "top_headlines", None) or [])[:3]],
                        }
                        self._save_held_cache(held)
                    self._exec_log["bought"].append({"symbol": sym, "price": price, "qty": qty})
                    self._flush_log()
                    self._append_history({"action": "BUY", "symbol": sym, "price": price, "qty": qty})
                    await self.bus.publish("trade.executed", {
                        "symbol": sym, "action": "BUY", "direction": "LONG",
                        "price": price, "qty": qty,
                        "prediction_score": pred.overall_score,
                    })
                    self.log.info("BUY %s  qty=%.3f  @$%.2f", sym, qty, price)

            elif action == "SHORT":
                price, qty = await loop.run_in_executor(
                    None, lambda: broker._place_market_short(
                        sym, pred.current_price, portfolio_value,
                        -pred.avg_sentiment, pred.bearish_count,
                    )
                )
                if qty:
                    async with self._held_cache_lock:
                        held = self._load_held_cache()
                        held[sym] = {
                            "type": "DAY", "direction": "SHORT",
                            "entered_at": datetime.now(_ET).isoformat(),
                            "score": pred.overall_score,
                            "rvol": getattr(pred, "volume_ratio", None),
                            "rsi": getattr(pred, "rsi", None),
                            "avg_sentiment": getattr(pred, "avg_sentiment", None),
                            "change_today_pct": getattr(pred, "change_today_pct", None),
                            "change_1w_pct": getattr(pred, "change_1w_pct", None),
                            "critic_verdict": data.get("critic_verdict", ""),
                            "approval_reason": data.get("reason", ""),
                            "top_headlines": [h[0][:100] for h in (getattr(pred, "top_headlines", None) or [])[:3]],
                        }
                        self._save_held_cache(held)
                    self._exec_log["shorted"].append({"symbol": sym, "price": price, "qty": qty})
                    self._flush_log()
                    self._append_history({"action": "SHORT", "symbol": sym, "price": price, "qty": qty})
                    await self.bus.publish("trade.executed", {
                        "symbol": sym, "action": "SHORT", "direction": "SHORT",
                        "price": price, "qty": qty,
                        "prediction_score": pred.overall_score,
                    })
                    self.log.info("SHORT %s  qty=%.3f  @$%.2f", sym, qty, price)

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
                async with self._held_cache_lock:
                    held = self._load_held_cache()
                    cache_entry = held.get(sym, {})
                hold_min = None
                try:
                    entered = datetime.fromisoformat(cache_entry["entered_at"])
                    hold_min = int((datetime.now(_ET) - entered).total_seconds() / 60)
                except Exception:
                    pass
                await loop.run_in_executor(None, broker._close_position_safely, sym)
                async with self._held_cache_lock:
                    held = self._load_held_cache()
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
                    "symbol": sym,
                    "action": "CLOSE",
                    "reason": reason,
                    "direction": direction,
                    "pnl": round(pnl, 2) if pnl else None,
                    "pnl_pct": round(pnl_pct, 2) if pnl_pct else None,
                    "prediction_score": pred.overall_score if pred else cache_entry.get("score", 0.0),
                    "critic_verdict": data.get("critic_verdict", cache_entry.get("critic_verdict", "")),
                    "hold_duration_min": hold_min,
                    "entry_price": entry_price or None,
                    "close_price": close_price or None,
                    "rvol": cache_entry.get("rvol"),
                    "rsi": cache_entry.get("rsi"),
                    "avg_sentiment": cache_entry.get("avg_sentiment"),
                    "change_today_pct": cache_entry.get("change_today_pct"),
                    "change_1w_pct": cache_entry.get("change_1w_pct"),
                    "top_headlines": cache_entry.get("top_headlines", []),
                    "approval_reason": cache_entry.get("approval_reason", ""),
                })
                self.log.info("CLOSE %s — %s", sym, reason)

        except Exception as exc:
            self.log.error("Execution error %s %s: %s", action, sym, exc)

    # ------------------------------------------------------------------
    # Periodic stop audit
    # ------------------------------------------------------------------

    async def _stop_audit_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_STOP_AUDIT_INTERVAL_S)
            broker = self._get_broker()
            if not broker.client:
                continue
            try:
                positions = await loop.run_in_executor(None, broker.client.get_all_positions)
                open_orders = await loop.run_in_executor(None, broker.client.get_orders)

                # Reconcile held_cache against live positions — catches manual liquidations
                live_syms = {p.symbol for p in positions}
                async with self._held_cache_lock:
                    held = self._load_held_cache()
                    removed = [s for s in list(held.keys()) if s not in live_syms]
                    for s in removed:
                        held.pop(s)
                    if removed:
                        self._save_held_cache(held)
                        self.log.info("Cache reconcile: removed manually-closed %s", ", ".join(removed))

                stopped_syms = {o.symbol for o in open_orders}
                for pos in positions:
                    if pos.symbol in stopped_syms:
                        continue
                    qty = abs(float(pos.qty))
                    is_crypto = "/" in pos.symbol
                    if not qty.is_integer() and not is_crypto:
                        continue  # no stop for fractional non-crypto positions
                    pnl_pct = float(pos.unrealized_plpc) * 100
                    is_long = _is_long_position(pos)
                    trail = 3.0 if pnl_pct >= 0 else 1.5
                    from alpaca.trading.requests import TrailingStopOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    req = TrailingStopOrderRequest(
                        symbol=pos.symbol,
                        qty=qty if is_crypto else int(qty),
                        side=OrderSide.SELL if is_long else OrderSide.BUY,
                        time_in_force=TimeInForce.GTC if is_crypto else TimeInForce.DAY,
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
        loop = asyncio.get_running_loop()
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
                eod_cache = self._load_held_cache()
                for pos in positions:
                    try:
                        close_price = float(pos.current_price)
                        close_qty = abs(float(pos.qty))  # preserve fractional qty for crypto
                        pnl = float(pos.unrealized_pl)
                        pnl_pct = float(pos.unrealized_plpc) * 100
                        eod_direction = "SHORT" if float(pos.qty) < 0 else "LONG"
                        cache_entry = eod_cache.get(pos.symbol, {})
                        hold_min = None
                        try:
                            entered = datetime.fromisoformat(cache_entry["entered_at"])
                            hold_min = int((datetime.now(_ET) - entered).total_seconds() / 60)
                        except Exception:
                            pass
                        await loop.run_in_executor(
                            None, broker._close_position_safely, pos.symbol
                        )
                        self._append_history({
                            "action": "CLOSE", "symbol": pos.symbol,
                            "price": close_price, "qty": close_qty,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
                        })
                        eod_cache.pop(pos.symbol, None)
                        self._save_held_cache(eod_cache)
                        await self.bus.publish("trade.closed", {
                            "symbol": pos.symbol,
                            "action": "CLOSE",
                            "reason": "EOD",
                            "direction": eod_direction,
                            "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "prediction_score": cache_entry.get("score", 0.0),
                            "critic_verdict": cache_entry.get("critic_verdict", ""),
                            "hold_duration_min": hold_min,
                            "rvol": cache_entry.get("rvol"),
                            "rsi": cache_entry.get("rsi"),
                            "avg_sentiment": cache_entry.get("avg_sentiment"),
                            "top_headlines": cache_entry.get("top_headlines", []),
                            "approval_reason": cache_entry.get("approval_reason", ""),
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
