"""CriticAgent — adversarial two-turn debate before any trade reaches Risk.

Subscribes to: symbol.predicted
Publishes to:  symbol.reviewed

Debate flow (Claude Sonnet):
  Turn 1 — Critic lists specific concerns about the trade thesis
  Turn 2 — Critic makes a final binding verdict given its own concerns

Verdicts:
  CONFIRM   — thesis solid, score unchanged
  DOWNGRADE — notable risks, score cut by 15 pts
  REJECT    — risks dominate, score capped at 45 (forces NEUTRAL, blocks entry)

CLOSE actions from MonitorAgent bypass the critic — speed matters for exits.
"""

import asyncio
import json
import os
import re
from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory

_SONNET_MODEL = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
_DOWNGRADE_DELTA = 15.0
_REJECT_CAP = 45.0

_SYSTEM_PROMPT = (
    "You are a skeptical risk analyst stress-testing trade proposals. "
    "Your job is to find reasons the trade could FAIL — not to agree with the predictor. "
    "Examine: second-order sector risks, narrative over-extension, weak volume confirmation, "
    "macro headwinds, earnings proximity, news the predictor may have ignored, "
    "and whether the options market (high P/C, elevated IV) implies institutional hedging. "
    "Be especially suspicious of BULLISH calls when VIX is elevated or SPY is declining."
)

_CONCERN_PROMPT = (
    "List your top 3 specific concerns about this trade. "
    "Be concrete — cite the actual data points that worry you. "
    "Do not give a verdict yet."
)

_VERDICT_PROMPT = (
    "Given your concerns above, make your final verdict. "
    "CONFIRM only if the thesis survives all three concerns. "
    "DOWNGRADE if concerns are real but not trade-blocking. "
    "REJECT if any concern is severe enough to invalidate the setup. "
    'Return ONLY valid JSON: {"verdict": "CONFIRM|DOWNGRADE|REJECT", '
    '"adjusted_score": <float 0-100>, "reasoning": "<one sentence>"}'
)


class CriticAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "CriticAgent")
        self._queue = bus.subscribe("symbol.predicted")
        self._memory = memory
        self._bedrock = None
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    def _get_bedrock(self):
        if self._bedrock is None:
            import boto3
            self._bedrock = boto3.client("bedrock-runtime", region_name=self._region)
        return self._bedrock

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, data: dict) -> None:
        sym = data["symbol"]
        pred = data["prediction"]
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._debate, pred)
            verdict = result.get("verdict", "CONFIRM")
            reasoning = result.get("reasoning", "")

            if verdict == "DOWNGRADE":
                adjusted = max(0.0, min(100.0, pred.overall_score - _DOWNGRADE_DELTA))
            elif verdict == "REJECT":
                adjusted = _REJECT_CAP
            else:
                adjusted = pred.overall_score

            rating = "BULLISH" if adjusted >= 60 else ("BEARISH" if adjusted <= 40 else "NEUTRAL")
            pred.overall_score = adjusted
            pred.confidence = adjusted
            pred.prediction = rating
            if reasoning:
                pred.reasoning.append(f"Critic ({verdict}): {reasoning}")

            self.log.info(
                "Critic: %s  %s → %.1f [%s]  concerns=%s",
                sym, verdict, adjusted, rating,
                result.get("concerns_summary", "—")[:60],
            )
            await self.bus.publish("symbol.reviewed", {
                "symbol": sym,
                "prediction": pred,
                "critic_verdict": verdict,
                "critic_reasoning": reasoning,
            })
        except Exception as exc:
            self.log.error("Critic error %s: %s", sym, exc)
            await self.bus.publish("symbol.reviewed", {
                "symbol": sym,
                "prediction": pred,
                "critic_verdict": "CONFIRM",
                "critic_reasoning": "critic unavailable",
            })

    # ------------------------------------------------------------------
    # Two-turn adversarial debate — runs in executor
    # ------------------------------------------------------------------

    def _debate(self, pred) -> dict:
        from .macro import MacroAgent
        from .research import ResearchAgent

        macro = MacroAgent.current
        research = ResearchAgent.get_cached(pred.symbol)

        headlines = (
            "\n".join(f"- {h[0]} ({h[2]})" for h in pred.top_headlines[:5])
            or "No recent news"
        )

        macro_block = ""
        if macro:
            macro_block = (
                f"\nMarket: {macro.get('regime')} | VIX={macro.get('vix', 0):.1f} | "
                f"SPY={macro.get('spy_change_pct', 0):+.1f}% | "
                f"Breadth={macro.get('breadth')} | "
                f"Lagging: {', '.join(macro.get('lagging_sectors', []))}"
            )

        research_block = ""
        if research:
            research_block = (
                f"\nResearch: RSI={research.get('rsi', 0):.0f} | "
                f"BB%={research.get('bb_pct', 0):.2f} | "
                f"P/C={research.get('put_call_ratio', 0):.2f} | "
                f"IV={research.get('implied_volatility', 0):.1%} | "
                f"Short={research.get('short_pct_float', 0):.1f}%float"
            )
            if research.get("synthesis"):
                research_block += f"\nResearch: {research['synthesis']}"

        proposal = (
            f"Trade: {pred.symbol} | {pred.archetype} | "
            f"Predictor={pred.prediction} (score={pred.overall_score:.1f})\n"
            f"RVOL={pred.volume_ratio:.1f}x | RSI={pred.rsi:.0f} | "
            f"1w={pred.change_1w_pct:+.1f}% 1m={pred.change_1m_pct:+.1f}% "
            f"3m={pred.change_3m_pct:+.1f}%\n"
            f"Predictor reasoning: {'; '.join(pred.reasoning[:3])}\n"
            f"Top headlines:\n{headlines}"
            f"{macro_block}{research_block}"
        )

        lessons = self._memory.format_for_prompt(archetype=pred.archetype)
        if lessons:
            proposal = lessons + "\n\n" + proposal

        bedrock = self._get_bedrock()

        # Turn 1: raise concerns
        try:
            r1 = bedrock.converse(
                modelId=_SONNET_MODEL,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[
                    {"role": "user", "content": [{"text": proposal + "\n\n" + _CONCERN_PROMPT}]}
                ],
                inferenceConfig={"maxTokens": 400, "temperature": 0.3},
            )
            c1 = r1.get("output", {}).get("message", {}).get("content", [])
            concerns_text = c1[0]["text"].strip() if c1 else "No concerns raised."
        except Exception as exc:
            self.log.warning("Critic turn-1 failed %s: %s", pred.symbol, exc)
            concerns_text = "Turn-1 unavailable."

        # Turn 2: final verdict given the concerns
        try:
            r2 = bedrock.converse(
                modelId=_SONNET_MODEL,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[
                    {"role": "user", "content": [{"text": proposal + "\n\n" + _CONCERN_PROMPT}]},
                    {"role": "assistant", "content": [{"text": concerns_text}]},
                    {"role": "user", "content": [{"text": _VERDICT_PROMPT}]},
                ],
                inferenceConfig={"maxTokens": 200, "temperature": 0.1},
            )
            c2 = r2.get("output", {}).get("message", {}).get("content", [])
            verdict_text = c2[0]["text"].strip() if c2 else ""
            if verdict_text.startswith("```"):
                verdict_text = re.sub(r"^```[a-z]*\n?", "", verdict_text).rstrip("`").strip()
            result = json.loads(verdict_text)
            result["concerns_summary"] = concerns_text[:200]
            return result
        except Exception as exc:
            self.log.warning("Critic turn-2 failed %s: %s", pred.symbol, exc)
            return {
                "verdict": "CONFIRM",
                "adjusted_score": pred.overall_score,
                "reasoning": "debate failed",
                "concerns_summary": concerns_text[:200],
            }
