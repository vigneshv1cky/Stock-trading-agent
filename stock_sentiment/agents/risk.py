"""RiskAgent — final gate before any order reaches ExecutorAgent.

Subscribes to: symbol.reviewed, position.alert
Publishes to:  trade.approved

All trade approval decisions are delegated to Haiku. The only code-level gates are:
  • BEARISH on existing long → immediate CLOSE (speed matters for exits)
  • Cooldown: 1-hour re-entry block after a stop-out (set by broker)

Haiku judges everything else: time of day, duplicate prevention, position caps,
displacement choice, buying power, shortability, VIX/regime, sector concentration,
and drawdown risk.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus
from .prompt_tuner import load_optimized_prompt

_COOLDOWN_FILE = os.path.expanduser("~/.stock_screener/cooldowns.json")
_HELD_CACHE = os.path.expanduser("~/.stock_screener/held_cache.json")

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_EARNINGS_SYSTEM_PROMPT = (
    "You are a trading system analyst deciding whether to HOLD or CLOSE an existing position "
    "immediately after an earnings announcement.\n\n"
    "For LONG positions:\n"
    "  • EPS beat > +5% with neutral or raised guidance → HOLD\n"
    "  • EPS miss < -5% OR lowered guidance OR negative management tone → CLOSE\n"
    "  • Inline (surprise within ±5%): use news headlines to break the tie\n"
    "For SHORT positions: reverse logic — beats are bad (CLOSE), misses are good (HOLD).\n\n"
    "News headlines carry the guidance tone and management commentary — weight them heavily. "
    "A large EPS beat with a stock dropping in after-hours means the market is reading something negative — CLOSE the long.\n\n"
    "Return ONLY valid JSON:\n"
    '{{"decision": "HOLD" or "CLOSE", "reasoning": "<one sentence>"}}'
)
_RISK_SYSTEM_PROMPT = (
    "You are a risk manager for an algorithmic swing trading system. "
    "Make a holistic APPROVE or BLOCK decision for the given trade signal.\n\n"
    "TIME OF DAY:\n"
    "  • First 15 min after open (before 9:45 AM ET): block most signals (gap-fill noise); "
    "approve only if score ≥ 70\n"
    "  • Last 60 min before close (after 3:00 PM ET): block new entries; "
    "approve only if score ≥ 80 (exceptional setup)\n\n"
    "POSITION LIMITS (soft targets: ≤8 total, ≤8 shorts):\n"
    "  • Already in this symbol → BLOCK (no doubling into same name)\n"
    "  • At the limit with displacement candidates → specify displace=SYMBOL "
    "if new signal beats it by ≥10 score pts; otherwise BLOCK\n"
    "  • At the limit with no displacement candidates → BLOCK\n\n"
    "FINANCIAL FACTS:\n"
    "  • Buying power < slot → BLOCK\n"
    "  • Shortable: NO → BLOCK the short\n\n"
    "MARKET CONDITIONS:\n"
    "  • VIX <15: calm — score ≥25 for longs, ≥40 for shorts\n"
    "  • VIX 15-25: normal — score ≥35 for longs, ≥50 for shorts\n"
    "  • VIX 25-35: elevated — score ≥50 for longs, ≥65 for shorts\n"
    "  • VIX >35: crisis — score ≥70 for longs; block most shorts\n"
    "  • RISK_OFF regime: raise thresholds ~15 pts; PANIC: raise ~25 pts or block\n"
    "  • RISK_ON with strong score: lean APPROVE\n"
    "  • Day P&L < −2%: block new longs; Day P&L < −4%: block all new entries\n"
    "  • Sector concentration >40% of portfolio: block more of that sector\n\n"
    "CRYPTO ASSETS (symbol contains '/', e.g. BTC/USD):\n"
    "  • LONG only — always BLOCK shorts on crypto\n"
    "  • No time-of-day gate (crypto trades 24/7)\n"
    "  • Score ≥50 required (higher noise floor vs equities)\n"
    "  • PANIC regime: BLOCK all new crypto entries\n"
    "  • Cap 2 crypto positions simultaneously\n\n"
    "Return ONLY valid JSON:\n"
    '{{"decision": "APPROVE" or "BLOCK", '
    '"displace": "<SYMBOL to close to make room, or null>", '
    '"reasoning": "<one sentence>"}}'
)


class RiskAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "RiskAgent")
        self._queue = bus.subscribe("symbol.reviewed", "position.alert")
        self._broker = None
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    def _get_broker(self):
        if self._broker is None:
            from stock_sentiment.agents.broker import PaperBroker
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
        critic_verdict = data.get("critic_verdict", "")
        loop = asyncio.get_running_loop()
        try:
            broker = self._get_broker()
            if not broker.client:
                return

            # ---- Fetch account and position state ----
            vix = await loop.run_in_executor(None, broker._get_vix)
            account = await loop.run_in_executor(None, broker.client.get_account)
            positions = await loop.run_in_executor(None, broker.client.get_all_positions)

            portfolio_value = float(account.equity)
            buying_power = float(account.buying_power)
            last_equity = float(account.last_equity)
            day_pnl_pct = (portfolio_value - last_equity) / last_equity * 100 if last_equity else 0.0

            from stock_sentiment.agents.broker import _is_long_position
            long_syms = {p.symbol for p in positions if _is_long_position(p)}
            short_syms = {p.symbol for p in positions if not _is_long_position(p)}
            held_syms = long_syms | short_syms

            # held_cache bridges the race-condition window between order submission
            # and Alpaca position confirmation — do not prune here.
            held_cache = self._load_held_cache()
            already_entered = held_syms | set(held_cache.keys())

            from .macro import MacroAgent
            macro = MacroAgent.current
            regime = macro.get("regime", "NEUTRAL") if macro else "NEUTRAL"

            from .portfolio import PortfolioAgent, get_sector
            portfolio_state = PortfolioAgent.current
            sym_sector = get_sector(sym)

            action: str | None = None
            block_reason = ""

            if pred.prediction == "BEARISH" and sym in long_syms:
                # Immediate exit — no LLM delay for closing a losing long
                action = "CLOSE"

            else:
                direction = "LONG" if pred.prediction == "BULLISH" else "SHORT"

                slot = broker._slot_size_for_score(
                    portfolio_value,
                    pred.avg_sentiment if direction == "LONG" else -pred.avg_sentiment,
                    pred.bullish_count if direction == "LONG" else pred.bearish_count,
                )

                # Fetch shortability as a broker fact to pass into LLM context
                shortable: bool | None = None
                if direction == "SHORT":
                    shortable = await loop.run_in_executor(None, broker._can_short, sym)

                context = self._build_context(
                    sym=sym, pred=pred, direction=direction,
                    now=datetime.now(_ET), vix=vix, regime=regime,
                    day_pnl_pct=day_pnl_pct, sym_sector=sym_sector,
                    portfolio_state=portfolio_state, held_syms=held_syms,
                    short_syms=short_syms, already_entered=already_entered,
                    held_cache=held_cache, buying_power=buying_power,
                    slot=slot, shortable=shortable,
                )

                llm_result = await loop.run_in_executor(
                    None, self._llm_risk_decision, sym, context
                )

                if llm_result["decision"] == "APPROVE":
                    action = "BUY" if direction == "LONG" else "SHORT"

                    displace_sym = llm_result.get("displace")
                    if displace_sym and displace_sym in held_cache:
                        held_cache.pop(displace_sym, None)
                        self._save_held_cache(held_cache)
                        await self.bus.publish("trade.approved", {
                            "symbol": displace_sym, "action": "CLOSE",
                            "prediction": None, "reason": "",
                            "portfolio_value": portfolio_value,
                        })
                        self.log.info("Displacement: closing %s for %s (%.1f)",
                                      displace_sym, sym, pred.overall_score)
                else:
                    block_reason = f"LLM: {llm_result.get('reasoning', 'blocked')}"

            if action and action != "CLOSE" and self._in_cooldown(sym):
                self.log.info("Cooldown blocked: %s", sym)
                return

            if action:
                reason = (
                    f"{pred.prediction} score={pred.overall_score:.1f} "
                    f"(VIX={vix:.1f} regime={regime})"
                    if action != "CLOSE"
                    else "BEARISH downgrade of long position"
                )
                self.log.info("Approved: %s %s — %s", sym, action, reason)
                await self.bus.publish("trade.approved", {
                    "symbol": sym, "action": action,
                    "prediction": pred, "reason": reason,
                    "portfolio_value": portfolio_value,
                    "critic_verdict": critic_verdict,
                })
            elif block_reason:
                self.log.info("Blocked %s [score=%.1f]: %s",
                              sym, pred.overall_score, block_reason)

        except Exception as exc:
            self.log.error("Risk error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Build comprehensive context string for LLM
    # ------------------------------------------------------------------

    @staticmethod
    def _build_context(
        sym: str,
        pred,
        direction: str,
        now: datetime,
        vix: float,
        regime: str,
        day_pnl_pct: float,
        sym_sector: str,
        portfolio_state: dict,
        held_syms: set,
        short_syms: set,
        already_entered: set,
        held_cache: dict,
        buying_power: float,
        slot: float,
        shortable: bool | None,
    ) -> str:
        minutes_since_open = (now.hour - 9) * 60 + (now.minute - 30)
        minutes_to_close = (16 * 60) - (now.hour * 60 + now.minute)

        cache_dir = "LONG" if direction == "LONG" else "SHORT"
        candidates = [
            f"{s}(score={d.get('score', 0):.0f})"
            for s, d in held_cache.items()
            if d.get("direction") == cache_dir
        ]

        sector_breakdown = portfolio_state.get("sector_concentration", {})
        top_sectors = sorted(sector_breakdown.items(), key=lambda x: x[1], reverse=True)[:5]
        sector_str = ", ".join(f"{s}={v:.0f}%" for s, v in top_sectors) or "none"

        financial = f"Buying power: ${buying_power:.0f} | Slot: ${slot:.0f}"
        if shortable is not None:
            financial += f" | {'Shortable: YES' if shortable else 'Shortable: NO'}"

        asset_note = " | Asset=CRYPTO" if "/" in sym else ""
        return (
            f"Trade: {sym}{asset_note} | Direction={direction} | Score={pred.overall_score:.1f}\n"
            f"Time: {now.strftime('%H:%M ET')} | "
            f"{minutes_since_open}min since open | {minutes_to_close}min to close\n"
            f"Already in position: {'YES' if sym in already_entered else 'no'}\n"
            f"Portfolio: {len(held_syms)} positions ({len(short_syms)} shorts) | "
            f"Soft limits: ≤8 total, ≤8 shorts\n"
            f"Displacement candidates ({direction}): "
            f"{', '.join(candidates) if candidates else 'none'}\n"
            f"{financial}\n"
            f"Market: VIX={vix:.1f} | Regime={regime} | Day P&L={day_pnl_pct:+.1f}%\n"
            f"Sector: {sym_sector} ({sector_breakdown.get(sym_sector, 0.0):.0f}% of portfolio) | "
            f"Breakdown: {sector_str}\n"
            f"Signal reasoning: {'; '.join(pred.reasoning[:2])}"
        )

    # ------------------------------------------------------------------
    # LLM risk decision — runs in executor (blocking Bedrock call)
    # ------------------------------------------------------------------

    def _llm_risk_decision(self, sym: str, context: str) -> dict:
        """Returns {"decision": "APPROVE"|"BLOCK", "displace": str|None, "reasoning": str}."""
        try:
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": load_optimized_prompt("risk_system", _RISK_SYSTEM_PROMPT)}],
                messages=[{"role": "user", "content": [{"text": context}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0.1},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            text = content[0]["text"].strip() if content else ""
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                result = json.loads(m.group())
                displace = result.get("displace")
                if displace in (None, "null", "", "none"):
                    result["displace"] = None
                self.log.debug(
                    "LLM risk %s → %s  displace=%s  (%s)",
                    sym, result.get("decision"), result.get("displace"),
                    result.get("reasoning"),
                )
                return result
        except Exception as exc:
            self.log.warning("LLM risk failed %s: %s — defaulting BLOCK", sym, exc)
        return {"decision": "BLOCK", "displace": None, "reasoning": "llm unavailable"}

    # ------------------------------------------------------------------
    # Position alert → CLOSE or re-trigger screener
    # ------------------------------------------------------------------

    async def _handle_alert(self, data: dict) -> None:
        sym = data["symbol"]
        alert_type = data["alert_type"]

        if alert_type == "EARNINGS_REPORTED":
            asyncio.create_task(self._analyze_earnings(data))
        elif alert_type == "REEVAL":
            await self.bus.publish("market.signal", {
                "symbol": sym, "price": 0, "rvol": 1.5,
                "price_change_pct": 0, "trigger_type": "REEVAL",
                "timestamp": datetime.now(_ET).isoformat(),
            })

    # ------------------------------------------------------------------
    # Earnings analysis — Haiku decides HOLD or CLOSE after results land
    # ------------------------------------------------------------------

    async def _analyze_earnings(self, data: dict) -> None:
        sym = data["symbol"]
        loop = asyncio.get_running_loop()
        try:
            broker = self._get_broker()
            if not broker.client:
                return

            positions = await loop.run_in_executor(None, broker.client.get_all_positions)
            pos = next((p for p in positions if p.symbol == sym), None)
            if not pos:
                return  # Position already closed

            from stock_sentiment.agents.broker import _is_long_position
            direction = "LONG" if _is_long_position(pos) else "SHORT"
            pnl_pct = float(pos.unrealized_plpc) * 100

            headlines = await loop.run_in_executor(None, self._fetch_earnings_news, sym)
            result = await loop.run_in_executor(
                None, self._llm_earnings_decision, sym, data, direction, pnl_pct, headlines
            )

            decision = result.get("decision", "HOLD")
            reasoning = result.get("reasoning", "")
            self.log.info("Earnings decision %s: %s — %s", sym, decision, reasoning)

            if decision == "CLOSE":
                account = await loop.run_in_executor(None, broker.client.get_account)
                await self.bus.publish("trade.approved", {
                    "symbol": sym, "action": "CLOSE",
                    "prediction": None,
                    "reason": f"Earnings analysis: {reasoning}",
                    "portfolio_value": float(account.equity),
                })
        except Exception as exc:
            self.log.error("Earnings analysis error %s: %s", sym, exc)

    def _fetch_earnings_news(self, sym: str) -> str:
        """Fetch last 24 h of news headlines for the earnings decision context."""
        try:
            import os as _os
            import datetime as _dt
            from datetime import timezone as _tz
            polygon_key = _os.environ.get("POLYGON_API_KEY", "")
            if not polygon_key:
                return "No news (POLYGON_API_KEY not set)"
            import polygon  # type: ignore[import-untyped]
            client = polygon.RESTClient(api_key=polygon_key)
            since = (_dt.datetime.now(_tz.utc) - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            articles = list(client.list_ticker_news(
                ticker=sym, published_utc_gte=since,
                limit=8, sort="published_utc", order="desc",
            ))
            if not articles:
                return "No recent news"
            lines = [
                f"- {a.title} ({a.publisher.name if a.publisher else '?'})"
                for a in articles[:8]
            ]
            return "\n".join(lines)
        except Exception as exc:
            self.log.debug("Earnings news fetch failed %s: %s", sym, exc)
            return "News fetch failed"

    def _llm_earnings_decision(
        self, sym: str, data: dict, direction: str, pnl_pct: float, headlines: str
    ) -> dict:
        reported = data.get("reported_eps")
        estimate = data.get("estimated_eps")
        surprise = data.get("surprise_pct", 0.0)

        eps_line = f"Reported EPS: {reported:.2f}" if reported is not None else "EPS: unknown"
        if estimate is not None:
            eps_line += f" vs estimate {estimate:.2f}  (surprise {surprise:+.1f}%)"

        context = (
            f"Symbol: {sym} | Direction: {direction} | Current P&L: {pnl_pct:+.1f}%\n"
            f"Earnings date: {data.get('earnings_date', 'today')}\n"
            f"{eps_line}\n\n"
            f"Recent news headlines (last 24 h):\n{headlines}"
        )
        try:
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _EARNINGS_SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": context}]}],
                inferenceConfig={"maxTokens": 150, "temperature": 0.1},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            text = content[0]["text"].strip() if content else ""
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as exc:
            self.log.warning("Earnings LLM failed %s: %s — defaulting HOLD", sym, exc)
        return {"decision": "HOLD", "reasoning": "llm unavailable"}

    # ------------------------------------------------------------------
    # Cache helpers
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
