"""LearningAgent — reflects on closed trade outcomes and rewrites AgentMemory lessons.

Triggers:
  • Every 10 closed trades (rolling)
  • Daily at 4:05 PM ET (after market close)

Each reflection cycle:
  1. Groups outcomes by archetype
  2. Sends last 50 outcomes to Claude Sonnet for pattern analysis with attribution
  3. Writes archetype-specific lessons to AgentMemory
  4. Triggers WeightOptimizer if ≥ 50 historical outcomes are in History

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
_SONNET_MODEL = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"

_SYSTEM_PROMPT = (
    "You are a trading system performance analyst performing root-cause attribution. "
    "Review recent trade outcomes grouped by archetype. For each archetype, identify "
    "2–3 systematic mistakes or patterns. Write concrete actionable instructions "
    "the predictor or critic should follow to avoid repeating them. "
    "Focus on: wrong archetype calls, missed macro/sector context, over-confidence "
    "in weak signals, cases where the critic should have blocked a trade, "
    "and patterns in the critic verdicts that were wrong. "
    "Return ONLY valid JSON:\n"
    '{"global_lessons": [{"pattern": "...", "instruction": "...", '
    '"win_rate": 0.0, "sample_size": 0}], '
    '"archetype_lessons": {"FRESH_BREAKOUT": [...], "BREAKOUT": [...], '
    '"MOMENTUM": [...], "RECOVERY": [...]}}'
)


class LearningAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "LearningAgent")
        self._queue = bus.subscribe("trade.closed")
        self._memory = memory
        self._buffer: list[dict] = []
        self._trades_since_reflection = 0
        self._bedrock = None
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    def _get_bedrock(self):
        if self._bedrock is None:
            import boto3
            self._bedrock = boto3.client("bedrock-runtime", region_name=self._region)
        return self._bedrock

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
        loop = asyncio.get_event_loop()
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
        loop = asyncio.get_event_loop()
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

            archetype_lessons: dict = result.get("archetype_lessons", {})
            for archetype, lessons in archetype_lessons.items():
                if lessons:
                    self._memory.update(
                        lessons,
                        trades_reviewed=len(self._buffer),
                        archetype=archetype,
                    )
                    total_lessons += len(lessons)

            if total_lessons:
                await self.bus.publish("memory.updated", {
                    "global_count": len(global_lessons),
                    "archetype_counts": {a: len(v) for a, v in archetype_lessons.items() if v},
                })
                self.log.info(
                    "Memory updated: %d global + %d archetype-specific lessons",
                    len(global_lessons),
                    sum(len(v) for v in archetype_lessons.values()),
                )

            await loop.run_in_executor(None, self._run_weight_optimizer)
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

        # Group by archetype for richer attribution
        by_archetype: dict[str, list[dict]] = {}
        for outcome in recent:
            arch = outcome.get("archetype", "UNKNOWN")
            by_archetype.setdefault(arch, []).append(outcome)

        archetype_summaries = []
        for arch, outcomes in by_archetype.items():
            rows = [
                f"  score={o.get('prediction_score', 0):.0f} "
                f"critic={o.get('critic_verdict', '?')} "
                f"action={o.get('action', '?')} "
                f"reason={o.get('reason', '?')}"
                for o in outcomes
            ]
            archetype_summaries.append(f"{arch} ({len(outcomes)} trades):\n" + "\n".join(rows))

        stats_note = ""
        if history_stats:
            stats_note = (
                f"\nBacktest: accuracy={history_stats.get('accuracy', 0):.1%} | "
                f"bullish_accuracy={history_stats.get('bullish_accuracy', 0):.1%} | "
                f"avg_5d_return={history_stats.get('avg_return_5d', 0):.2f}%"
            )

        user_text = (
            f"Recent {len(recent)} trade outcomes by archetype:\n\n"
            + "\n\n".join(archetype_summaries)
            + stats_note
        )

        try:
            resp = self._get_bedrock().converse(
                modelId=_SONNET_MODEL,
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

    def _run_weight_optimizer(self) -> None:
        try:
            from stock_sentiment.history import History
            from stock_sentiment.market.weight_optimizer import WeightOptimizer
            hist = History()
            WeightOptimizer(hist).optimize()
            hist.close()
        except Exception as exc:
            self.log.warning("Weight optimizer skipped: %s", exc)
