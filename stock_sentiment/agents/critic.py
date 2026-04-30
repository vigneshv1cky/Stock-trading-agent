"""CriticAgent — adversarial two-turn debate before any trade reaches Risk.

Subscribes to: symbol.predicted
Publishes to:  symbol.reviewed

Improvements over original bot version:
  • 5-tier verdict system replacing the blunt CONFIRM / DOWNGRADE / REJECT:
      UPGRADE   — Predictor was too conservative; push score up by 8 pts (max 85)
      CONFIRM   — thesis solid; score unchanged
      CAUTION   — real but non-fatal concerns; −8 pts
      DOWNGRADE — notable risks; Critic sets the exact adjusted_score
      REJECT    — severe risk; score hard-capped at 32 (forces BEARISH/NEUTRAL, blocks entry)
  • Critic's adjusted_score is trusted (not overridden with fixed math),
    validated only to stay within tier bounds
  • Two-turn debate: Turn 1 raises concerns, Turn 2 gives binding verdict
  • CLOSE actions bypass the critic — speed matters for exits
"""

import asyncio
import json
import os
import re

from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_REJECT_CAP = 32.0
_UPGRADE_MAX = 85.0
_UPGRADE_DELTA = 8.0
_CAUTION_DELTA = 8.0

_SYSTEM_PROMPT = (
    "You are a skeptical risk analyst stress-testing trade proposals. "
    "Your job is to find reasons the trade could FAIL — not to agree with the predictor. "
    "Examine: second-order sector risks, narrative over-extension, weak volume confirmation, "
    "macro headwinds, earnings proximity, whether the options market (high P/C, elevated IV) "
    "implies institutional hedging, and short-squeeze risk inflating a BEARISH setup. "
    "Be especially suspicious of BULLISH calls when VIX is elevated or SPY is declining. "
    "Only UPGRADE if the predictor clearly undersold a high-conviction setup."
)

_CONCERN_PROMPT = (
    "List your top 3 specific concerns about this trade. "
    "Cite the actual data points. Do not give a verdict yet."
)

_VERDICT_PROMPT = (
    "Given your concerns, choose the most appropriate verdict:\n"
    "  UPGRADE   — predictor too conservative, setup genuinely strong (score rises ≤8 pts, max 85)\n"
    "  CONFIRM   — thesis survives all concerns, score unchanged\n"
    "  CAUTION   — real but non-fatal risks (score drops ~8 pts)\n"
    "  DOWNGRADE — notable risks clearly present; set an exact adjusted_score\n"
    "  REJECT    — at least one concern is trade-blocking (score capped at 32)\n\n"
    "Return ONLY valid JSON:\n"
    '{"verdict": "UPGRADE|CONFIRM|CAUTION|DOWNGRADE|REJECT", '
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
        llm_confidence = float(data.get("llm_confidence", 50.0))
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, self._debate, pred, llm_confidence)
            verdict = result.get("verdict", "CONFIRM")
            reasoning = result.get("reasoning", "")
            raw_adjusted = float(result.get("adjusted_score", pred.overall_score))

            original_score = pred.overall_score
            adjusted = self._apply_verdict(verdict, original_score, raw_adjusted)
            rating = "BULLISH" if adjusted >= 60 else ("BEARISH" if adjusted <= 40 else "NEUTRAL")

            pred.overall_score = adjusted
            pred.confidence = adjusted
            pred.prediction = rating
            if reasoning:
                pred.reasoning.append(f"Critic ({verdict}): {reasoning}")

            self.log.info(
                "Critic: %s  %s  %.1f→%.1f [%s]",
                sym, verdict, original_score, adjusted, rating,
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
    # Verdict → score mapping (validated within tier bounds)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_verdict(verdict: str, original: float, critic_adjusted: float) -> float:
        # NEUTRAL predictions (41–59) cannot be rejected into BEARISH territory —
        # a failed/uncertain predictor signal should never trigger a close or short
        is_neutral = 41.0 <= original <= 59.0
        if verdict == "UPGRADE":
            upgraded = original + _UPGRADE_DELTA
            return min(upgraded, _UPGRADE_MAX)
        if verdict == "CONFIRM":
            return original
        if verdict == "CAUTION":
            return max(0.0, original - _CAUTION_DELTA)
        if verdict == "DOWNGRADE":
            if is_neutral:
                return original  # can't downgrade a neutral signal further
            lo = max(0.0, min(critic_adjusted, original - 5.0))
            return max(0.0, min(lo, 100.0))
        if verdict == "REJECT":
            if is_neutral:
                return original  # REJECT on NEUTRAL → treat as CONFIRM
            return min(original, _REJECT_CAP)
        return original

    # ------------------------------------------------------------------
    # Two-turn adversarial debate — runs in executor
    # ------------------------------------------------------------------

    def _debate(self, pred, llm_confidence: float) -> dict:
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
                f"Lagging sectors: {', '.join(macro.get('lagging_sectors', []))}"
            )

        research_block = ""
        if research:
            research_block = (
                f"\nResearch: RSI={research.get('rsi', 0):.0f} | "
                f"BB%={research.get('bb_pct', 0):.2f} | "
                f"P/C={research.get('put_call_ratio', 0):.2f} | "
                f"IV={research.get('implied_volatility', 0):.1%} | "
                f"Short={research.get('short_pct_float', 0):.1f}% of float"
            )
            if research.get("synthesis"):
                research_block += f"\nResearch: {research['synthesis']}"

        proposal = (
            f"Trade: {pred.symbol} | {pred.archetype} | "
            f"Predictor={pred.prediction} score={pred.overall_score:.1f} "
            f"confidence={llm_confidence:.0f}\n"
            f"RVOL={pred.volume_ratio:.1f}x | RSI={pred.rsi:.0f} | "
            f"1w={pred.change_1w_pct:+.1f}% 1m={pred.change_1m_pct:+.1f}% "
            f"3m={pred.change_3m_pct:+.1f}%\n"
            f"Predictor reasoning: {'; '.join(pred.reasoning[:3])}\n"
            f"Headlines:\n{headlines}"
            f"{macro_block}{research_block}"
        )

        lessons = self._memory.format_for_prompt(archetype=pred.archetype)
        if lessons:
            proposal = lessons + "\n\n" + proposal

        bedrock = self._get_bedrock()

        # Turn 1 — raise concerns
        try:
            r1 = bedrock.converse(
                modelId=_HAIKU_MODEL,
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

        # Turn 2 — binding verdict given the concerns
        try:
            r2 = bedrock.converse(
                modelId=_HAIKU_MODEL,
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
            m = re.search(r"\{.*\}", verdict_text, re.DOTALL)
            if not m:
                raise ValueError("No JSON found in verdict")
            result = json.loads(m.group())
            result["concerns_summary"] = concerns_text[:200]
            return result
        except Exception as exc:
            self.log.warning("Critic turn-2 failed %s: %s", pred.symbol, exc)
            return {
                "verdict": "CONFIRM",
                "adjusted_score": pred.overall_score,
                "reasoning": "debate failed",
            }
