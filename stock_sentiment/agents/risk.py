"""RiskAgent — final gate before any order reaches ExecutorAgent.

Subscribes to: symbol.reviewed, position.alert
Publishes to:  trade.approved

Improvements over original bot version:
  • Market-hours gate: no new entries in first 15 min (9:30–9:45) or last 15 min of session
  • Portfolio drawdown circuit breaker: halt new LONG entries if day P&L < –2 %
  • Earnings lookahead: 4–7 days away → require score 5 pts above threshold (soft penalty)
  • Same-sector penalty: already hold a position in same sector → require score 5 pts higher
  • Macro overlay: RISK_OFF +5, PANIC +10 on buy threshold (unchanged from prior)
  • All blocks are logged with reason so LearningAgent can learn from them
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

_COOLDOWN_FILE = os.path.expanduser("~/.stock_screener/cooldowns.json")
_HELD_CACHE = os.path.expanduser("~/.stock_screener/held_cache.json")
_MAX_POSITIONS = 10
_MAX_SHORTS = 8
_SHORT_MAX_SCORE = 35
_SECTOR_CONCENTRATION_BLOCK = 50.0   # % — hard block
_SECTOR_PENALTY_PTS = 5.0            # extra pts required when same sector held
_EARNINGS_SOFT_DAYS = 7              # days — require +5 pts above threshold
_DRAWDOWN_HALT_PCT = -2.0            # % — halt new longs below this daily P&L
_NO_ENTRY_OPEN_MIN = 15              # skip first 15 min (price discovery)
_NO_ENTRY_CLOSE_MIN = 60             # skip last 60 min — no new entries after 3:00 PM
_DISPLACEMENT_MARGIN = 5.0           # new signal must beat worst held position by this many pts


class RiskAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "RiskAgent")
        self._queue = bus.subscribe("symbol.reviewed", "position.alert")
        self._broker = None

    def _get_broker(self):
        if self._broker is None:
            from stock_sentiment.market.broker import PaperBroker
            self._broker = PaperBroker()
        return self._broker

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            topic = msg["topic"]
            data = msg["data"]
            if topic == "symbol.reviewed":
                asyncio.create_task(self._evaluate(data))
            elif topic == "position.alert":
                asyncio.create_task(self._handle_alert(data))

    # ------------------------------------------------------------------
    # New prediction → BUY / SHORT / block
    # ------------------------------------------------------------------

    async def _evaluate(self, data: dict) -> None:
        sym = data["symbol"]
        pred = data["prediction"]
        loop = asyncio.get_event_loop()
        try:
            broker = self._get_broker()
            if not broker.client:
                return

            # ---- Market-hours gate ----
            now = datetime.now(_ET)
            minutes_since_open = (now.hour - 9) * 60 + (now.minute - 30)
            minutes_to_close = (16 * 60) - (now.hour * 60 + now.minute)
            if minutes_since_open < _NO_ENTRY_OPEN_MIN:
                self.log.info("Hours gate (open): %s at %s", sym, now.strftime("%H:%M"))
                return
            if minutes_to_close < _NO_ENTRY_CLOSE_MIN:
                self.log.info("Hours gate (close): %s at %s", sym, now.strftime("%H:%M"))
                return

            # ---- Fetch account state ----
            vix = await loop.run_in_executor(None, broker._get_vix)
            threshold = broker._threshold_from_vix(vix)
            account = await loop.run_in_executor(None, broker.client.get_account)
            positions = await loop.run_in_executor(None, broker.client.get_all_positions)

            portfolio_value = float(account.equity)
            cash = float(account.cash)
            last_equity = float(account.last_equity)
            day_pnl_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0
            slot = broker._slot_size_for_score(portfolio_value)

            from stock_sentiment.market.broker import _is_long_position
            long_syms = {p.symbol for p in positions if _is_long_position(p)}
            short_syms = {p.symbol for p in positions if not _is_long_position(p)}
            held_syms = long_syms | short_syms

            # held_cache is updated synchronously by ExecutorAgent on every fill —
            # use it as the authoritative cap check to avoid Alpaca latency race conditions
            held_cache = self._load_held_cache()
            held_syms = held_syms | set(held_cache.keys())
            cache_shorts = {s for s, v in held_cache.items() if v.get("direction") == "SHORT"}
            short_syms = short_syms | cache_shorts

            # ---- Macro overlay ----
            from .macro import MacroAgent
            macro = MacroAgent.current
            regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"
            if regime == "RISK_OFF":
                threshold = min(threshold + 5, 85)
            elif regime == "PANIC":
                threshold = min(threshold + 10, 90)

            # ---- Earnings soft penalty ----
            days_to_earn = getattr(pred, "days_to_earnings", None)
            if days_to_earn is not None and _EARNINGS_SOFT_DAYS >= days_to_earn > 3:
                threshold += 5.0
                self.log.debug("Earnings penalty +5 for %s (%dd)", sym, days_to_earn)

            # ---- Same-sector penalty ----
            from .portfolio import PortfolioAgent, get_sector
            portfolio_state = PortfolioAgent.current
            sym_sector = get_sector(sym)
            sector_conc = portfolio_state.get("sector_concentration", {}).get(sym_sector, 0.0)
            if sector_conc > 0:
                threshold += _SECTOR_PENALTY_PTS
                self.log.debug("Sector penalty +%.0f for %s (%s=%.0f%%)",
                               _SECTOR_PENALTY_PTS, sym, sym_sector, sector_conc)

            is_crypto = "/" in sym
            # Crypto: lower buy threshold (more volatile, smaller moves count)
            if is_crypto:
                threshold = min(threshold, 50)

            action: str | None = None
            block_reason = ""

            if pred.prediction == "BULLISH" and pred.overall_score >= threshold:
                if sym in held_syms:
                    return

                # Score-based displacement: at cap, evict weakest long if new signal is stronger
                displacement_long: tuple[str, float] | None = None
                if len(held_syms) >= _MAX_POSITIONS:
                    displacement_long = self._find_displacement_target(
                        "BUY", pred.overall_score, held_cache
                    )
                    if displacement_long:
                        held_cache.pop(displacement_long[0], None)
                        self._save_held_cache(held_cache)
                        held_syms.discard(displacement_long[0])

                # Drawdown circuit breaker — halt new longs on bad days
                if day_pnl_pct < _DRAWDOWN_HALT_PCT:
                    block_reason = (
                        f"drawdown halt (day P&L={day_pnl_pct:.1f}% < {_DRAWDOWN_HALT_PCT}%)"
                    )
                elif len(held_syms) >= _MAX_POSITIONS:
                    block_reason = f"position cap ({_MAX_POSITIONS})"
                elif cash < slot:
                    block_reason = f"insufficient cash (${cash:.0f} < ${slot:.0f})"
                elif self._sector_blocked(sym_sector, portfolio_state):
                    block_reason = f"sector concentration ({sym_sector}={sector_conc:.0f}%)"
                else:
                    action = "BUY"
                    if displacement_long:
                        d_sym, d_score = displacement_long
                        await self.bus.publish("trade.approved", {
                            "symbol": d_sym, "action": "CLOSE", "prediction": None,
                            "reason": "",
                            "portfolio_value": portfolio_value,
                        })
                        self.log.info(
                            "Displacement: closing %s (%.1f) for %s (%.1f)",
                            d_sym, d_score, sym, pred.overall_score,
                        )

            elif pred.prediction == "BEARISH":
                if sym in long_syms:
                    action = "CLOSE"
                elif is_crypto:
                    pass  # no short selling for crypto
                elif (
                    pred.overall_score <= _SHORT_MAX_SCORE
                    and sym not in held_syms
                ):
                    # Score-based displacement: at cap, evict weakest short if new signal is stronger
                    displacement_short: tuple[str, float] | None = None
                    if len(held_syms) >= _MAX_POSITIONS:
                        displacement_short = self._find_displacement_target(
                            "SHORT", pred.overall_score, held_cache
                        )
                        if displacement_short:
                            held_cache.pop(displacement_short[0], None)
                            self._save_held_cache(held_cache)
                            held_syms.discard(displacement_short[0])
                            short_syms.discard(displacement_short[0])

                    if len(short_syms) >= _MAX_SHORTS:
                        block_reason = f"short cap ({_MAX_SHORTS})"
                    elif len(held_syms) >= _MAX_POSITIONS:
                        block_reason = f"position cap ({_MAX_POSITIONS})"
                    elif cash < slot:
                        block_reason = "insufficient cash"
                    elif self._sector_blocked(sym_sector, portfolio_state):
                        block_reason = f"sector concentration ({sym_sector}={sector_conc:.0f}%)"
                    else:
                        can_short = await loop.run_in_executor(None, broker._can_short, sym)
                        if can_short:
                            action = "SHORT"
                            if displacement_short:
                                d_sym, d_score = displacement_short
                                await self.bus.publish("trade.approved", {
                                    "symbol": d_sym, "action": "CLOSE", "prediction": None,
                                    "reason": "",
                                    "portfolio_value": portfolio_value,
                                })
                                self.log.info(
                                    "Displacement: closing %s (%.1f) for %s (%.1f)",
                                    d_sym, d_score, sym, pred.overall_score,
                                )

            if action and action != "CLOSE" and self._in_cooldown(sym):
                self.log.info("Cooldown blocked: %s", sym)
                return

            if action:
                reason = (
                    f"{pred.prediction} score={pred.overall_score:.1f} ≥ {threshold:.0f}"
                    f" (VIX={vix:.1f} regime={regime})"
                    if action != "CLOSE"
                    else "BEARISH downgrade of long position"
                )
                self.log.info("Approved: %s %s — %s", sym, action, reason)
                await self.bus.publish("trade.approved", {
                    "symbol": sym,
                    "action": action,
                    "prediction": pred,
                    "reason": reason,
                    "portfolio_value": portfolio_value,
                })
            elif block_reason:
                self.log.info("Blocked %s [score=%.1f threshold=%.0f]: %s",
                              sym, pred.overall_score, threshold, block_reason)

        except Exception as exc:
            self.log.error("Risk error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Sector concentration hard block
    # ------------------------------------------------------------------

    @staticmethod
    def _sector_blocked(sector: str, portfolio_state: dict) -> bool:
        total = portfolio_state.get("total_count", 0)
        if total < 3:
            return False
        conc = portfolio_state.get("sector_concentration", {}).get(sector, 0.0)
        return conc >= _SECTOR_CONCENTRATION_BLOCK

    # ------------------------------------------------------------------
    # Position alert → CLOSE or re-trigger screener
    # ------------------------------------------------------------------

    async def _handle_alert(self, data: dict) -> None:
        sym = data["symbol"]
        alert_type = data["alert_type"]

        if alert_type == "EARNINGS":
            await self.bus.publish("trade.approved", {
                "symbol": sym,
                "action": "CLOSE",
                "prediction": None,
                "reason": data["detail"],
                "portfolio_value": 0,
            })
        elif alert_type == "REEVAL":
            await self.bus.publish("market.signal", {
                "symbol": sym,
                "price": 0,
                "rvol": 1.5,
                "price_change_pct": 0,
                "trigger_type": "REEVAL",
                "timestamp": datetime.now(_ET).isoformat(),
            })

    # ------------------------------------------------------------------
    # Cooldown check
    # ------------------------------------------------------------------

    @staticmethod
    def _load_held_cache() -> dict:
        try:
            if os.path.exists(_HELD_CACHE):
                with open(_HELD_CACHE) as fh:
                    return json.load(fh)
        except Exception:
            pass
        return {}

    @staticmethod
    def _save_held_cache(cache: dict) -> None:
        try:
            os.makedirs(os.path.dirname(_HELD_CACHE), exist_ok=True)
            with open(_HELD_CACHE, "w") as fh:
                json.dump(cache, fh, indent=2)
        except Exception:
            pass

    @staticmethod
    def _find_displacement_target(
        action: str, new_score: float, held_cache: dict
    ) -> tuple[str, float] | None:
        """Return (symbol, score) of the weakest same-direction position to displace, or None."""
        if action == "BUY":
            candidates = {
                sym: data.get("score", 50.0)
                for sym, data in held_cache.items()
                if data.get("direction") == "LONG"
            }
            if not candidates:
                return None
            worst = min(candidates, key=candidates.__getitem__)
            if new_score >= candidates[worst] + _DISPLACEMENT_MARGIN:
                return worst, candidates[worst]
        elif action == "SHORT":
            candidates = {
                sym: data.get("score", 50.0)
                for sym, data in held_cache.items()
                if data.get("direction") == "SHORT"
            }
            if not candidates:
                return None
            worst = max(candidates, key=candidates.__getitem__)
            if new_score <= candidates[worst] - _DISPLACEMENT_MARGIN:
                return worst, candidates[worst]
        return None

    def _in_cooldown(self, sym: str) -> bool:
        try:
            if not os.path.exists(_COOLDOWN_FILE):
                return False
            with open(_COOLDOWN_FILE) as fh:
                cooldowns: dict = json.load(fh)
            if sym not in cooldowns:
                return False
            blocked_until = datetime.fromisoformat(cooldowns[sym])
            if blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=_ET)
            return datetime.now(_ET) < blocked_until
        except Exception:
            return False
