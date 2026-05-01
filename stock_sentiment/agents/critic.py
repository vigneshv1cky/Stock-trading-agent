"""CriticAgent — adversarial two-turn debate before any trade reaches Risk.

Subscribes to: symbol.predicted
Publishes to:  symbol.reviewed

  • Turn 1: raise top 3 concerns, cite data, no score yet
  • Turn 2: output adjusted_score directly — LLM owns the adjustment magnitude
  • CLOSE actions bypass the critic — speed matters for exits
"""

import asyncio
import json
import os
import re

from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory
from .prompt_tuner import load_optimized_prompt

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = (
    "You are a skeptical risk analyst stress-testing trade proposals. "
    "Your job is to find reasons the trade could FAIL — not to agree with the predictor. "
    "Examine: second-order sector risks, narrative over-extension, weak volume confirmation, "
    "macro headwinds, earnings proximity, RSI extremes suggesting exhaustion, "
    "and whether ATR/volatility implies the move is already over. "
    "Be especially suspicious of BULLISH calls when VIX is elevated or SPY is declining. "
    "Only raise the score if the predictor clearly undersold a high-conviction setup. "
    "Scores range from -100 to 100: positive = BULLISH, negative = BEARISH. Be decisive — avoid scores near 0."
)

_CONCERN_PROMPT = (
    "List your top 3 specific concerns about this trade. "
    "Cite the actual data points. Do not give a score yet."
)

_SCORE_PROMPT = (
    "Given your concerns above, what should the adjusted score be?\n"
    "The predictor gave {original:.0f}.\n"
    "- Concerns are minor or unfounded → keep it close or push higher if undersold\n"
    "- Concerns are real but survivable → lower it proportionally\n"
    "- At least one concern is trade-blocking → push it below -36\n\n"
    "Return ONLY valid JSON:\n"
    '{{"adjusted_score": <float -100 to 100>, "reasoning": "<one sentence>"}}'
)


class CriticAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "CriticAgent")
        self._queue = bus.subscribe("symbol.predicted")
        self._memory = memory
        self._region = os.environ.get("AWS_REGION", "us-east-1")

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
            reasoning = result.get("reasoning", "")
            raw_adjusted = float(result.get("adjusted_score", pred.overall_score))

            original_score = pred.overall_score
            adjusted = max(-100.0, min(100.0, raw_adjusted))
            rating = "BULLISH" if adjusted >= 0 else "BEARISH"

            pred.overall_score = adjusted
            pred.confidence = adjusted
            pred.prediction = rating
            if reasoning:
                pred.reasoning.append(f"Critic: {reasoning}")

            self.log.info(
                "Critic: %s  %.1f→%.1f [%s]",
                sym, original_score, adjusted, rating,
            )
            await self.bus.publish("symbol.reviewed", {
                "symbol": sym,
                "prediction": pred,
                "critic_verdict": rating,
                "critic_reasoning": reasoning,
            })
        except Exception as exc:
            self.log.error("Critic error %s: %s", sym, exc)
            await self.bus.publish("symbol.reviewed", {
                "symbol": sym,
                "prediction": pred,
                "critic_verdict": pred.prediction,
                "critic_reasoning": "critic unavailable",
            })


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
                f"MACD_hist={research.get('macd_hist', 0):+.4f} | "
                f"ATR={research.get('atr_pct', 0):.1f}% | "
                f"vol_trend={research.get('vol_trend', 0):+.1%}"
            )
            if research.get("synthesis"):
                research_block += f"\nResearch: {research['synthesis']}"

        proposal = (
            f"Trade: {pred.symbol} | "
            f"Predictor={pred.prediction} score={pred.overall_score:.1f} "
            f"confidence={llm_confidence:.0f}\n"
            f"RVOL={pred.volume_ratio:.1f}x | RSI={pred.rsi:.0f} | "
            f"1w={pred.change_1w_pct:+.1f}% 1m={pred.change_1m_pct:+.1f}% "
            f"3m={pred.change_3m_pct:+.1f}%\n"
            f"Predictor reasoning: {'; '.join(pred.reasoning[:3])}\n"
            f"Headlines:\n{headlines}"
            f"{macro_block}{research_block}"
        )

        lessons = self._memory.format_for_prompt()
        if lessons:
            proposal = lessons + "\n\n" + proposal

        bedrock = self.get_bedrock(self._region)
        active_system = load_optimized_prompt("critic_system", _SYSTEM_PROMPT)

        # Turn 1 — raise concerns
        try:
            r1 = bedrock.converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": active_system}],
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
                system=[{"text": active_system}],
                messages=[
                    {"role": "user", "content": [{"text": proposal + "\n\n" + _CONCERN_PROMPT}]},
                    {"role": "assistant", "content": [{"text": concerns_text}]},
                    {"role": "user", "content": [{"text": _SCORE_PROMPT.format(original=pred.overall_score)}]},
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
