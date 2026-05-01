"""PredictorAgent — Claude Haiku LLM scoring (no formula blend).

Subscribes to: symbol.analysed
Publishes to:  symbol.predicted

  • LLM receives full quantitative context (RVOL, RSI, BB, MACD, price history, news)
  • LLM returns score + confidence + red_flag_severity (NONE/MINOR/MODERATE/FATAL)
  • Red flag severity: MINOR=no cap, MODERATE=cap 0, FATAL=cap -44 (hard block)
  • Confidence gate: LLM confidence < 35 → skip publish (signal not actionable)
  • PANIC regime: BULLISH signals dropped entirely (score ≥ 0 → skip publish)
  • No NEUTRAL rating — every published signal is BULLISH or BEARISH
  • Structured 4-step reasoning prompt
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory
from .prompt_tuner import load_optimized_prompt

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Red flag severity caps
_SEVERITY_CAPS = {"NONE": 100, "MINOR": 100, "MODERATE": 0, "FATAL": -44}

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
    "STEP 4 – FINAL SCORE: Synthesise steps 1-3 into a -100 to 100 score and a confidence level.\n\n"
    "Scoring: 0=neutral, >20=BULLISH, <-20=BEARISH.\n"
    "Assess the signal type yourself from the data: breakout, dip-buy, momentum, mean-reversion, or volume event. "
    "For sharp intraday drops with company-specific bad news (earnings miss, guidance cut, downgrade): score strongly BEARISH. "
    "If news is ABSENT, lean BEARISH unless RVOL ≥ 5x — unexplained volume spikes alone rarely justify a long.\n"
    "Scoring: positive = BULLISH bias, negative = BEARISH bias. Be decisive — avoid scores near 0.\n"
    "red_flag_severity: NONE | MINOR (no cap) | MODERATE (score capped 0) | FATAL (trade-blocking).\n"
    "confidence: 0-100, how certain you are in your score given available data.\n"
    'Return ONLY valid JSON: {"score": <-100 to 100>, "confidence": <0-100>, '
    '"red_flag_severity": "NONE|MINOR|MODERATE|FATAL", "reasoning": "<2 sentences max>"}'
)



@dataclass
class StockPrediction:
    symbol: str
    current_price: float
    change_3m_pct: float
    change_1m_pct: float
    change_1w_pct: float
    prediction: str
    confidence: float
    overall_score: float
    reasoning: list[str]
    momentum_score: float
    sentiment_score: float
    technical_score: float
    volume_score: float
    volume_ratio: float
    avg_sentiment: float
    bullish_count: int
    bearish_count: int
    top_headlines: list
    rsi: float
    days_to_earnings: Optional[int]
    predicted_move: str
    change_today_pct: float = 0.0


class PredictorAgent(BaseAgent):
    def __init__(self, bus: EventBus, memory: AgentMemory):
        super().__init__(bus, "PredictorAgent")
        self._queue = bus.subscribe("symbol.analysed")
        self._memory = memory
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, data: dict) -> None:
        sym = data["symbol"]
        stock = data["screened_stock"]
        articles = data["scored_articles"]
        loop = asyncio.get_running_loop()
        try:
            from .macro import MacroAgent
            from .research import ResearchAgent
            macro_ctx = MacroAgent.current
            research = ResearchAgent.get_cached(sym)
            sub = self._compute_sub_scores(stock, articles, research)

            llm = await loop.run_in_executor(
                None, self._llm_score, stock, articles, sub, macro_ctx, research
            )

            llm_score = float(llm.get("score", 0.0))
            llm_confidence = float(llm.get("confidence", 50.0))
            severity = llm.get("red_flag_severity", "NONE")
            score = llm_score

            # Apply red flag severity cap
            cap = _SEVERITY_CAPS.get(severity, 100)
            score = min(score, float(cap))

            # Confidence gate: AI not certain enough → skip this signal entirely
            if llm_confidence < _MIN_CONFIDENCE:
                self.log.debug("Low confidence (%.0f) — skipping %s", llm_confidence, sym)
                return

            # Macro: drop BULLISH signals in PANIC regime entirely
            regime = macro_ctx.get("regime", "NEUTRAL") if macro_ctx else "NEUTRAL"
            if regime == "PANIC" and score >= 0:
                self.log.debug("PANIC regime — dropping BULLISH signal for %s", sym)
                return

            rating = "BULLISH" if score >= 0 else "BEARISH"

            reasoning: list[str] = [
                f"RVOL: {stock.volume_ratio:.1f}x  Today: {stock.change_today_pct:+.1f}%",
                f"LLM score={llm_score:.0f} confidence={llm_confidence:.0f}",
            ]
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

            prediction = self._build_prediction(stock, sub, score, rating, reasoning)
            self.log.info(
                "Predicted: %s  %s  score=%.1f  conf=%.0f  severity=%s",
                sym, rating, score, llm_confidence, severity,
            )
            await self.bus.publish("symbol.predicted", {
                "symbol": sym,
                "prediction": prediction,
                "llm_confidence": llm_confidence,
            })
        except Exception as exc:
            self.log.error("Predictor error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # LLM call — Claude Haiku via Bedrock
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
                f"vol_trend={research.get('vol_trend', 0):+.1%}"
            )
            if research.get("synthesis"):
                research_block += f"\nResearch synthesis: {research['synthesis']}"

        signal = ""
        if hasattr(stock, "change_today_pct"):
            signal = f"Signal: RVOL={stock.volume_ratio:.1f}x | Today={stock.change_today_pct:+.1f}%"

        user_text = (
            f"Current time: {now.strftime('%Y-%m-%d %H:%M ET')}\n"
            f"{macro_block}{research_block}\n\n"
            f"Stock: {stock.symbol}{earnings_note}\n"
            f"{signal} | 1w={stock.change_1w_pct:+.1f}% | "
            f"1m={stock.change_1m_pct:+.1f}% | 3m={stock.change_3m_pct:+.1f}%\n"
            f"Sentiment: avg={sub['avg_sentiment']:+.2f} | "
            f"bullish_1h={sub['bullish_count']} | bearish_1h={sub['bearish_count']}\n\n"
            f"Headlines (newest first):\n  {headlines}"
        )

        lessons = self._memory.format_for_prompt()
        if lessons:
            user_text = lessons + "\n\n" + user_text

        try:
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": load_optimized_prompt("predictor_system", _SYSTEM_PROMPT)}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": 600, "temperature": 0},
            )
            return self._parse_response(resp, 0.0)
        except Exception as exc:
            self.log.warning("LLM fallback %s: %s", stock.symbol, exc)
            return {
                "score": 0.0,
                "confidence": 25.0,  # below _MIN_CONFIDENCE → signal skipped
                "red_flag_severity": "NONE",
                "reasoning": "",
            }

    def _parse_response(self, resp: dict, fallback_score: float) -> dict:
        content = resp.get("output", {}).get("message", {}).get("content", [])
        text = next(
            (b.get("text", "").strip() for b in content if b.get("text")),
            "",
        )
        if not text:
            return {
                "score": fallback_score,
                "confidence": 40.0,
                "red_flag_severity": "NONE",
                "reasoning": "",
            }
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("No JSON object found in response")
        result = json.loads(m.group())
        if "red_flag_severity" not in result:
            result["red_flag_severity"] = "FATAL" if result.get("red_flag") else "NONE"
        return result

    def refresh_memory(self, memory: Optional[AgentMemory] = None) -> None:
        if memory is not None:
            self._memory = memory

    # ------------------------------------------------------------------
    # Sub-score computation and prediction building
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_sub_scores(stock, articles, research: Optional[dict]) -> dict:
        avg_sentiment = 0.0
        bullish_count = 0
        bearish_count = 0
        top_headlines = []
        if articles:
            scores = [a.normalized_score for a in articles]
            avg_sentiment = sum(scores) / len(scores)
            bullish_count = sum(1 for a in articles if a.normalized_score > 0.2)
            bearish_count = sum(1 for a in articles if a.normalized_score < -0.2)
            top_headlines = [
                (a.article.title, a.normalized_score, a.article.source, a.article.url)
                for a in sorted(articles, key=lambda a: a.article.published_at, reverse=True)[:5]
            ]

        sent_score = min(100.0, (avg_sentiment + 1) * 50 + (15 if bullish_count >= 3 else 0))
        rsi = float(research.get("rsi", 50.0)) if research else 50.0

        return {
            "mom_score": 0.0, "vol_score": 0.0, "tech_score": 0.0,
            "sent_score": sent_score, "rsi": rsi,
            "avg_sentiment": avg_sentiment, "bullish_count": bullish_count, "bearish_count": bearish_count,
            "top_headlines": top_headlines,
        }

    @staticmethod
    def _build_prediction(stock, sub: dict, score: float, rating: str, reasoning: list[str]) -> "StockPrediction":
        return StockPrediction(
            symbol=stock.symbol, current_price=stock.current_price,
            change_3m_pct=stock.change_3m_pct, change_1m_pct=stock.change_1m_pct, change_1w_pct=stock.change_1w_pct,
            prediction=rating, confidence=score, overall_score=score,
            momentum_score=sub["mom_score"], sentiment_score=sub["sent_score"],
            technical_score=sub["tech_score"], volume_score=sub["vol_score"],
            volume_ratio=stock.volume_ratio,
            avg_sentiment=sub["avg_sentiment"], bullish_count=sub["bullish_count"], bearish_count=sub["bearish_count"],
            top_headlines=sub["top_headlines"], rsi=sub["rsi"],
            days_to_earnings=stock.days_to_earnings,
            predicted_move=("+5-12% (Oversold Bounce)" if rating == "BULLISH" and sub["rsi"] < 35 else "+3-7% (Standard)"),
            reasoning=reasoning,
            change_today_pct=stock.change_today_pct,
        )
