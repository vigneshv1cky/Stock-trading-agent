"""LearningAgent — reflects on closed trade outcomes and rewrites AgentMemory lessons.

Triggers:
  • Every 10 closed trades (rolling)
  • Daily at 4:05 PM ET (after market close)

Each reflection cycle:
  1. Sends last 50 outcomes to Claude Haiku for pattern analysis
  2. Writes global lessons to AgentMemory
  3. Triggers WeightOptimizer if ≥ 50 historical outcomes are in History

Subscribes to: trade.closed
Publishes to:  memory.updated
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
from .memory import AgentMemory

_MIN_TRADES_FOR_REFLECTION = 10
_MAX_BUFFER = 100
_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = (
    "You are a trading system performance analyst performing root-cause attribution. "
    "Review recent trade outcomes and identify 3–5 systematic patterns or mistakes. "
    "Write concrete actionable instructions the predictor or critic should follow.\n\n"
    "Each outcome includes: symbol, direction (LONG/SHORT), prediction score, critic verdict, "
    "P&L%, hold duration (minutes), exit reason, RVOL at entry, RSI at entry, "
    "avg sentiment, regime/VIX at entry, and top headlines at entry.\n\n"
    "Analyse across all dimensions:\n"
    "  • Score ranges that reliably win or lose by direction\n"
    "  • Regime/VIX combinations that precede losses\n"
    "  • Hold duration: stopped out <30min = wrong entry; held >2h = good setup\n"
    "  • RSI extremes at entry correlating with quick stop-outs\n"
    "  • Critic verdicts that were wrong (e.g. BULLISH verdict but trade lost)\n"
    "  • News patterns — headlines that looked bullish but preceded losses\n"
    "  • RVOL at entry: does low RVOL correlate with failure?\n"
    "  • Sentiment vs outcome mismatch\n"
    "Return ONLY valid JSON:\n"
    '{"global_lessons": [{"pattern": "...", "instruction": "...", "win_rate": 0.0, "sample_size": 0}]}'
)


class LearningAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "LearningAgent")
        self._queue = bus.subscribe("trade.closed")
        self._memory = memory
        self._buffer: list[dict] = []
        self._trades_since_reflection = 0
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(
            self._collect_outcomes(),
            self._daily_reflection_loop(),
        )

    # ------------------------------------------------------------------
    # Collect closed-trade outcomes
    # ------------------------------------------------------------------

    async def _collect_outcomes(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            msg = await self._queue.get()
            outcome = msg["data"]
            self._buffer.append(outcome)
            if len(self._buffer) > _MAX_BUFFER:
                self._buffer = self._buffer[-_MAX_BUFFER:]

            self._trades_since_reflection += 1
            if self._trades_since_reflection >= _MIN_TRADES_FOR_REFLECTION:
                self._trades_since_reflection = 0
                await self._reflect(loop)

    # ------------------------------------------------------------------
    # Daily 4:05 PM ET reflection
    # ------------------------------------------------------------------

    async def _daily_reflection_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(60)
            now = datetime.now(_ET)
            if now.hour == 16 and now.minute == 5 and self._buffer:
                await self._reflect(loop)
                await asyncio.sleep(3600)

    # ------------------------------------------------------------------
    # Reflection cycle
    # ------------------------------------------------------------------

    async def _reflect(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._buffer:
            return
        self.log.info("Reflecting on %d trade outcomes…", len(self._buffer))
        try:
            history_stats = await loop.run_in_executor(None, self._get_history_stats)
            result = await loop.run_in_executor(None, self._call_llm, history_stats)

            total_lessons = 0
            global_lessons = result.get("global_lessons", [])
            if global_lessons:
                self._memory.update(global_lessons, trades_reviewed=len(self._buffer))
                total_lessons += len(global_lessons)

            if total_lessons:
                await self.bus.publish("memory.updated", {"global_count": len(global_lessons)})
                self.log.info("Memory updated: %d lessons", len(global_lessons))

        except Exception as exc:
            self.log.error("Reflection error: %s", exc)

    # ------------------------------------------------------------------
    # Blocking helpers
    # ------------------------------------------------------------------

    def _get_history_stats(self) -> dict:
        try:
            from stock_sentiment.history import History
            hist = History()
            stats = hist.get_backtest_stats()
            hist.close()
            return stats
        except Exception:
            return {}

    def _call_llm(self, history_stats: dict) -> dict:
        recent = self._buffer[-50:]

        rows = []
        for o in recent:
            pnl = o.get("pnl_pct")
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "pnl=?"
            headlines = "; ".join((o.get("top_headlines") or [])[:2])
            row = (
                f"sym={o.get('symbol', '?')} dir={o.get('direction', '?')} "
                f"score={o.get('prediction_score', 0):.0f} critic={o.get('critic_verdict', '?')} "
                f"pnl={pnl_str} hold={o.get('hold_duration_min', '?')}min "
                f"exit={o.get('reason', '?')} rvol={o.get('rvol', '?')} "
                f"rsi={o.get('rsi', '?')} sent={o.get('avg_sentiment', '?')} "
                f"regime={o.get('regime_at_entry', '?')} vix={o.get('vix_at_entry', '?')}"
            )
            if headlines:
                row += f" | news=[{headlines[:120]}]"
            rows.append(row)

        stats_note = ""
        if history_stats:
            stats_note = (
                f"\nBacktest: accuracy={history_stats.get('accuracy', 0):.1%} | "
                f"bullish_accuracy={history_stats.get('bullish_accuracy', 0):.1%} | "
                f"avg_5d_return={history_stats.get('avg_return_5d', 0):.2f}%"
            )

        user_text = (
            f"Recent {len(recent)} trade outcomes:\n" + "\n".join(rows) + stats_note
        )

        try:
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": 2048, "temperature": 0.3},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            text = content[0]["text"].strip() if content else ""
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
            return json.loads(text)
        except Exception as exc:
            self.log.warning("Reflection LLM failed: %s", exc)
            return {}

