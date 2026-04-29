"""RiskAgent — gates every signal through VIX regime, position limits, and cooldowns.

Subscribes to: symbol.reviewed, position.alert
Publishes to:  trade.approved

Upgrades vs. prior version:
  • Reads PortfolioAgent.current for sector concentration — blocks trades that
    would push a single sector above 50% of open positions
  • Reads MacroAgent.current to tighten thresholds during RISK_OFF / PANIC
  • Logs reason for every block so LearningAgent has richer data to learn from
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
_MAX_POSITIONS = 10
_MAX_SHORTS = 8
_SHORT_MAX_SCORE = 35
_SECTOR_CONCENTRATION_BLOCK = 50.0   # % — block new positions that worsen this


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
    # New prediction → decide BUY / SHORT / block
    # ------------------------------------------------------------------

    async def _evaluate(self, data: dict) -> None:
        sym = data["symbol"]
        pred = data["prediction"]
        loop = asyncio.get_event_loop()
        try:
            broker = self._get_broker()
            if not broker.client:
                return

            vix = await loop.run_in_executor(None, broker._get_vix)
            threshold = broker._threshold_from_vix(vix)

            # Macro overlay: tighten threshold during RISK_OFF / PANIC
            from .macro import MacroAgent
            macro = MacroAgent.current
            if macro:
                regime = macro.get("regime", "NEUTRAL")
                if regime == "RISK_OFF":
                    threshold = min(threshold + 5, 85)
                elif regime == "PANIC":
                    threshold = min(threshold + 10, 90)

            account = await loop.run_in_executor(None, broker.client.get_account)
            positions = await loop.run_in_executor(None, broker.client.get_all_positions)

            portfolio_value = float(account.equity)
            cash = float(account.cash)
            slot = broker._slot_size_for_score(portfolio_value)

            from stock_sentiment.market.broker import _is_long_position
            long_syms = {p.symbol for p in positions if _is_long_position(p)}
            short_syms = {p.symbol for p in positions if not _is_long_position(p)}
            held_syms = long_syms | short_syms

            action: str | None = None
            block_reason: str = ""

            if pred.prediction == "BULLISH" and pred.overall_score >= threshold:
                if sym in held_syms:
                    return
                if len(held_syms) >= _MAX_POSITIONS:
                    block_reason = f"position cap ({_MAX_POSITIONS})"
                elif cash < slot:
                    block_reason = f"insufficient cash (${cash:.0f} < ${slot:.0f})"
                elif self._sector_blocked(sym, direction="LONG"):
                    block_reason = f"sector concentration ≥{_SECTOR_CONCENTRATION_BLOCK:.0f}%"
                else:
                    action = "BUY"

            elif pred.prediction == "BEARISH":
                if sym in long_syms:
                    action = "CLOSE"
                elif (
                    pred.overall_score <= _SHORT_MAX_SCORE
                    and sym not in held_syms
                ):
                    if len(short_syms) >= _MAX_SHORTS:
                        block_reason = f"short cap ({_MAX_SHORTS})"
                    elif len(held_syms) >= _MAX_POSITIONS:
                        block_reason = f"position cap ({_MAX_POSITIONS})"
                    elif cash < slot:
                        block_reason = "insufficient cash"
                    elif self._sector_blocked(sym, direction="SHORT"):
                        block_reason = f"sector concentration ≥{_SECTOR_CONCENTRATION_BLOCK:.0f}%"
                    else:
                        can_short = await loop.run_in_executor(None, broker._can_short, sym)
                        if can_short:
                            action = "SHORT"

            if action and action != "CLOSE" and self._in_cooldown(sym):
                self.log.info("Cooldown blocked: %s", sym)
                return

            if action:
                reason = (
                    f"{pred.prediction} score={pred.overall_score:.1f} ≥ {threshold:.0f}"
                    f" (VIX={vix:.1f})"
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
                self.log.info("Blocked %s: %s", sym, block_reason)

        except Exception as exc:
            self.log.error("Risk error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Portfolio concentration gate
    # ------------------------------------------------------------------

    def _sector_blocked(self, sym: str, direction: str = "") -> bool:  # noqa: ARG002
        from .portfolio import PortfolioAgent, get_sector
        state = PortfolioAgent.current
        total = state.get("total_count", 0)
        if total < 3:
            return False  # don't gate on tiny portfolios

        sector = get_sector(sym)
        conc = state.get("sector_concentration", {}).get(sector, 0.0)
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
