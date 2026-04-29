"""PredictorAgent — formula sub-scores + Claude Sonnet with optional extended thinking.

Subscribes to: symbol.analysed
Publishes to:  symbol.predicted

Improvements over original bot version:
  • Adaptive formula/LLM blend: more research data → trust LLM more (up to 65 %)
  • LLM returns score + confidence + red_flag_severity (NONE/MINOR/MODERATE/FATAL)
  • Red flag severity: MINOR=no cap, MODERATE=cap 50, FATAL=cap 28 (hard block)
  • Confidence gate: LLM confidence < 35 → force NEUTRAL regardless of score
  • Macro-adjusted output: PANIC regime suppresses BULLISH signals entirely
  • Extended thinking band widened to 35–70 (was 38–65)
  • Structured 4-step reasoning prompt instead of open-ended
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from stock_sentiment.market.price_fetcher import PriceFetcher
from stock_sentiment.market.stock_predictor import StockPredictor
from stock_sentiment.market.technicals import TechnicalAnalyzer

from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory

_SONNET_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_THINKING_LOW = 35.0
_THINKING_HIGH = 70.0
_THINKING_BUDGET = 2000

# Red flag severity caps
_SEVERITY_CAPS = {"NONE": 100, "MINOR": 100, "MODERATE": 50, "FATAL": 28}

# Minimum LLM confidence to publish a directional signal
_MIN_CONFIDENCE = 35.0

_SYSTEM_PROMPT = (
    "You are a quantitative equity analyst scoring a single stock for intraday swing trading. "
    "Follow this exact 4-step reasoning structure:\n"
    "STEP 1 – TECHNICAL THESIS: Is the price/volume setup genuine? "
    "Assess: RVOL confirmation, BB position, MACD momentum, RSI overbought/oversold risk.\n"
    "STEP 2 – CATALYST QUALITY: Do the headlines explain or contradict the move? "
    "Weight today's news heavily. Flag second-order sector risks.\n"
    "STEP 3 – RISK FACTORS: List the top 2 risks. "
    "Consider: earnings proximity, high short interest squeeze risk, elevated IV, macro regime.\n"
    "STEP 4 – FINAL SCORE: Synthesise steps 1-3 into a 0-100 score and a confidence level.\n\n"
    "Scoring: 50=neutral, >60=BULLISH, <40=BEARISH.\n"
    "red_flag_severity: NONE | MINOR (no cap) | MODERATE (score capped 50) | FATAL (trade-blocking).\n"
    "confidence: 0-100, how certain you are in your score given available data.\n"
    'Return ONLY valid JSON: {"score": <0-100>, "confidence": <0-100>, '
    '"red_flag_severity": "NONE|MINOR|MODERATE|FATAL", "reasoning": "<2 sentences max>"}'
)


class PredictorAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "PredictorAgent")
        self._queue = bus.subscribe("symbol.analysed")
        self._memory = memory
        self._predictor = StockPredictor()
        self._tech = TechnicalAnalyzer()
        self._price_fetcher = PriceFetcher(cache_ttl_seconds=600)
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
        stock = data["screened_stock"]
        articles = data["scored_articles"]
        loop = asyncio.get_event_loop()
        try:
            prices = await loop.run_in_executor(
                None, lambda: self._price_fetcher.fetch_batch([sym], period="3mo")
            )
            ti = self._tech.analyze(prices[sym]) if sym in prices else None
            sub = self._predictor._compute_sub_scores(stock, articles, ti)

            from .macro import MacroAgent
            from .research import ResearchAgent
            macro_ctx = MacroAgent.current
            research = ResearchAgent.get_cached(sym)

            llm = await loop.run_in_executor(
                None, self._llm_score, stock, articles, sub, macro_ctx, research
            )

            formula_score = sub["formula_score"]
            llm_score = float(llm.get("score", formula_score))
            llm_confidence = float(llm.get("confidence", 50.0))
            severity = llm.get("red_flag_severity", "NONE")
            used_thinking = bool(llm.get("used_thinking", False))

            # Adaptive blend: richer research data → trust LLM more
            research_richness = self._research_richness(research)
            llm_weight = 0.50 + research_richness * 0.15   # 0.50 → 0.65
            formula_weight = 1.0 - llm_weight
            score = formula_score * formula_weight + llm_score * llm_weight

            # Apply red flag severity cap
            cap = _SEVERITY_CAPS.get(severity, 100)
            score = min(score, float(cap))

            # Confidence gate: low confidence → neutral zone
            if llm_confidence < _MIN_CONFIDENCE:
                score = max(41.0, min(score, 59.0))  # force NEUTRAL band

            # Macro: suppress BULLISH in PANIC regime
            regime = macro_ctx.get("regime", "NEUTRAL") if macro_ctx else "NEUTRAL"
            if regime == "PANIC" and score >= 60:
                score = 59.0   # push to NEUTRAL — no new longs in PANIC

            rating = "BULLISH" if score >= 60 else ("BEARISH" if score <= 40 else "NEUTRAL")

            reasoning: list[str] = [
                f"Archetype: {stock.archetype}",
                f"RVOL: {stock.volume_ratio:.1f}x",
                f"Blend: formula={formula_score:.0f} llm={llm_score:.0f} "
                f"(w={llm_weight:.0%}) confidence={llm_confidence:.0f}",
            ]
            if stock.days_to_earnings is not None:
                reasoning.append(f"Earnings in {stock.days_to_earnings}d")
            if macro_ctx:
                reasoning.append(
                    f"Macro: {regime} | VIX={macro_ctx.get('vix', 0):.1f}"
                )
            if research and research.get("synthesis"):
                reasoning.append(f"Research: {research['synthesis'][:120]}")
            if llm.get("reasoning"):
                reasoning.append(llm["reasoning"])
            if severity != "NONE":
                reasoning.append(f"Red flag [{severity}] — score capped at {cap}")
            if used_thinking:
                reasoning.append("Extended thinking applied")

            prediction = self._predictor._build_prediction(stock, sub, score, rating, reasoning)
            self.log.info(
                "Predicted: %s  %s  score=%.1f  conf=%.0f  severity=%s  thinking=%s",
                sym, rating, score, llm_confidence, severity, used_thinking,
            )
            await self.bus.publish("symbol.predicted", {
                "symbol": sym,
                "prediction": prediction,
                "llm_confidence": llm_confidence,
            })
        except Exception as exc:
            self.log.error("Predictor error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Research richness score 0.0–1.0 (how complete is the research data)
    # ------------------------------------------------------------------

    @staticmethod
    def _research_richness(research: Optional[dict]) -> float:
        if not research:
            return 0.0
        fields = ["rsi", "bb_pct", "macd_hist", "put_call_ratio", "short_pct_float", "synthesis"]
        present = sum(1 for f in fields if research.get(f))
        return present / len(fields)

    # ------------------------------------------------------------------
    # LLM call — Sonnet with optional extended thinking
    # ------------------------------------------------------------------

    def _llm_score(
        self,
        stock,
        articles,
        sub: dict,
        macro_ctx: dict,
        research: Optional[dict],
    ) -> dict:
        now = datetime.now(_ET)
        sorted_arts = sorted(articles, key=lambda a: a.article.published_at, reverse=True)[:5]

        if sorted_arts:
            parts: list[str] = []
            for art in sorted_arts:
                age_h = (now - art.article.published_at).total_seconds() / 3600
                age_str = f"{age_h * 60:.0f}m ago" if age_h < 1 else f"{age_h:.0f}h ago"
                parts.append(
                    f'[{age_str}] "{art.article.title[:180]}" ({art.article.source})'
                )
            headlines = "\n  ".join(parts)
        else:
            headlines = "No recent news"

        earnings_note = (
            f" | Earnings in {stock.days_to_earnings}d"
            if stock.days_to_earnings is not None and stock.days_to_earnings <= 14
            else ""
        )

        macro_block = ""
        if macro_ctx:
            macro_block = (
                f"\nMarket regime: {macro_ctx.get('regime')} | "
                f"VIX={macro_ctx.get('vix', 0):.1f} | "
                f"SPY={macro_ctx.get('spy_change_pct', 0):+.1f}% | "
                f"QQQ={macro_ctx.get('qqq_change_pct', 0):+.1f}% | "
                f"Breadth={macro_ctx.get('breadth')} | "
                f"Leading: {', '.join(macro_ctx.get('leading_sectors', []))} | "
                f"Lagging: {', '.join(macro_ctx.get('lagging_sectors', []))}"
            )

        research_block = ""
        if research:
            research_block = (
                f"\nResearch: RSI={research.get('rsi', 0):.0f} | "
                f"BB%={research.get('bb_pct', 0):.2f} (0=lower band, 1=upper) | "
                f"MACD_hist={research.get('macd_hist', 0):+.4f} | "
                f"ATR={research.get('atr_pct', 0):.1f}% | "
                f"vol_trend={research.get('vol_trend', 0):+.1%} | "
                f"P/C={research.get('put_call_ratio', 0):.2f} | "
                f"IV={research.get('implied_volatility', 0):.1%} | "
                f"Short={research.get('short_pct_float', 0):.1f}% of float "
                f"({research.get('short_ratio', 0):.1f}d to cover)"
            )
            if research.get("synthesis"):
                research_block += f"\nResearch synthesis: {research['synthesis']}"

        signal = ""
        if hasattr(stock, "change_today_pct"):
            signal = f"Signal: RVOL={stock.volume_ratio:.1f}x | Today={stock.change_today_pct:+.1f}%"

        user_text = (
            f"Current time: {now.strftime('%Y-%m-%d %H:%M ET')}\n"
            f"{macro_block}{research_block}\n\n"
            f"Stock: {stock.symbol} | Archetype: {stock.archetype}{earnings_note}\n"
            f"{signal}\n"
            f"Performance: 1w={stock.change_1w_pct:+.1f}% | "
            f"1m={stock.change_1m_pct:+.1f}% | 3m={stock.change_3m_pct:+.1f}%\n"
            f"Formula sub-scores: {sub}\n\n"
            f"Headlines (newest first):\n  {headlines}"
        )

        lessons = self._memory.format_for_prompt(archetype=stock.archetype)
        if lessons:
            user_text = lessons + "\n\n" + user_text

        formula_score = sub["formula_score"]
        use_thinking = _THINKING_LOW <= formula_score <= _THINKING_HIGH

        try:
            if use_thinking:
                return self._call_with_thinking(user_text, formula_score)
            return self._call_standard(user_text, formula_score)
        except Exception as exc:
            self.log.warning("LLM fallback %s: %s", stock.symbol, exc)
            return {
                "score": formula_score,
                "confidence": 25.0,  # below _MIN_CONFIDENCE → forces NEUTRAL, no trade
                "red_flag_severity": "NONE",
                "reasoning": "",
            }

    def _call_standard(self, user_text: str, fallback_score: float) -> dict:
        resp = self._get_bedrock().converse(
            modelId=_SONNET_MODEL,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"maxTokens": 600, "temperature": 0},
        )
        return self._parse_response(resp, fallback_score, used_thinking=False)

    def _call_with_thinking(self, user_text: str, fallback_score: float) -> dict:
        resp = self._get_bedrock().converse(
            modelId=_SONNET_MODEL,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"maxTokens": _THINKING_BUDGET + 600, "temperature": 1},
            additionalModelRequestFields={
                "thinking": {"type": "enabled", "budget_tokens": _THINKING_BUDGET}
            },
        )
        return self._parse_response(resp, fallback_score, used_thinking=True)

    def _parse_response(self, resp: dict, fallback_score: float, used_thinking: bool) -> dict:
        content = resp.get("output", {}).get("message", {}).get("content", [])
        text = ""
        for block in content:
            if block.get("type") == "text" or ("text" in block and "thinking" not in block):
                text = block.get("text", "").strip()
                break
        if not text:
            return {
                "score": fallback_score,
                "confidence": 40.0,
                "red_flag_severity": "NONE",
                "reasoning": "",
            }
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        # Haiku sometimes appends explanatory text after the JSON object — extract just the first object
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("No JSON object found in response")
        result = json.loads(m.group())
        # Normalise severity
        if "red_flag_severity" not in result:
            result["red_flag_severity"] = "FATAL" if result.get("red_flag") else "NONE"
        result["used_thinking"] = used_thinking
        return result

    def refresh_memory(self, memory: Optional[AgentMemory] = None) -> None:
        if memory is not None:
            self._memory = memory
