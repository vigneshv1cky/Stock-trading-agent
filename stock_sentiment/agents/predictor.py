"""PredictorAgent — formula sub-scores + Claude Sonnet with optional extended thinking.

Subscribes to: symbol.analysed
Publishes to:  symbol.predicted

Upgrades vs. prior version:
  • Claude Sonnet 4.6 (was Haiku) for richer reasoning
  • Extended thinking enabled when score is in the uncertain 38–65 band
  • Consumes MacroAgent.current for live market regime context
  • Merges ResearchAgent cached results (options, advanced technicals, short interest)
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
from stock_sentiment.market.stock_predictor import StockPredictor, _SYSTEM_PROMPT
from stock_sentiment.market.technicals import TechnicalAnalyzer

from .base import BaseAgent
from .event_bus import EventBus
from .memory import AgentMemory

_SONNET_MODEL = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"

# Scores in this band trigger extended thinking (genuinely uncertain calls)
_THINKING_LOW = 38.0
_THINKING_HIGH = 65.0
_THINKING_BUDGET = 2000   # tokens


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

            # Pull live context from sibling agents
            from .macro import MacroAgent
            from .research import ResearchAgent
            macro_ctx = MacroAgent.current
            research = ResearchAgent.get_cached(sym)

            llm = await loop.run_in_executor(
                None, self._llm_score, stock, articles, sub, macro_ctx, research
            )

            formula_score = sub["formula_score"]
            llm_qual = float(llm.get("score", formula_score))
            red_flag = bool(llm.get("red_flag", False))
            used_thinking = bool(llm.get("used_thinking", False))

            score = formula_score * 0.50 + llm_qual * 0.50
            if red_flag:
                score = min(score, 35.0)

            rating = "BULLISH" if score >= 60 else ("BEARISH" if score <= 40 else "NEUTRAL")
            reasoning: list[str] = [
                f"Archetype: {stock.archetype}",
                f"RVOL: {stock.volume_ratio:.1f}x",
            ]
            if stock.days_to_earnings is not None:
                reasoning.append(f"Earnings in {stock.days_to_earnings}d")
            if macro_ctx:
                reasoning.append(
                    f"Macro: {macro_ctx.get('regime')} | VIX={macro_ctx.get('vix', 0):.1f}"
                )
            if research and research.get("synthesis"):
                reasoning.append(f"Research: {research['synthesis'][:120]}")
            if llm.get("reasoning"):
                reasoning.append(llm["reasoning"])
            if red_flag:
                reasoning.append("RED FLAG: negative news override")
            if used_thinking:
                reasoning.append("Extended thinking applied")

            prediction = self._predictor._build_prediction(stock, sub, score, rating, reasoning)
            self.log.info(
                "Predicted: %s  %s  score=%.1f  thinking=%s",
                sym, rating, score, used_thinking,
            )
            await self.bus.publish("symbol.predicted", {
                "symbol": sym,
                "prediction": prediction,
            })
        except Exception as exc:
            self.log.error("Predictor error %s: %s", sym, exc)

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
                f"Leading: {', '.join(macro_ctx.get('leading_sectors', []))}"
            )

        research_block = ""
        if research:
            research_block = (
                f"\nResearch: RSI={research.get('rsi', 0):.0f} | "
                f"BB%={research.get('bb_pct', 0):.2f} | "
                f"MACD_hist={research.get('macd_hist', 0):+.4f} | "
                f"ATR={research.get('atr_pct', 0):.1f}% | "
                f"P/C={research.get('put_call_ratio', 0):.2f} | "
                f"IV={research.get('implied_volatility', 0):.1%} | "
                f"Short={research.get('short_pct_float', 0):.1f}%float"
            )
            if research.get("synthesis"):
                research_block += f"\nResearch synthesis: {research['synthesis']}"

        user_text = (
            f"Current time: {now.strftime('%Y-%m-%d %H:%M ET')}\n"
            f"{macro_block}{research_block}\n\n"
            f"1. {stock.symbol} | {stock.archetype}{earnings_note}\n"
            f"   Headlines (newest first):\n  {headlines}"
        )

        lessons = self._memory.format_for_prompt(archetype=stock.archetype)
        if lessons:
            user_text = lessons + "\n\n" + user_text

        # Decide whether to use extended thinking
        formula_score = sub["formula_score"]
        use_thinking = _THINKING_LOW <= formula_score <= _THINKING_HIGH

        try:
            if use_thinking:
                return self._call_with_thinking(stock.symbol, user_text, formula_score)
            else:
                return self._call_standard(stock.symbol, user_text, formula_score)
        except Exception as exc:
            self.log.warning("LLM fallback %s: %s", stock.symbol, exc)
            return {"score": formula_score, "reasoning": "", "red_flag": False}

    def _call_standard(self, _sym: str, user_text: str, fallback_score: float) -> dict:
        resp = self._get_bedrock().converse(
            modelId=_SONNET_MODEL,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0},
        )
        return self._parse_response(resp, fallback_score, used_thinking=False)

    def _call_with_thinking(self, sym: str, user_text: str, fallback_score: float) -> dict:
        self.log.debug("Extended thinking for %s (formula=%.1f)", sym, fallback_score)
        resp = self._get_bedrock().converse(
            modelId=_SONNET_MODEL,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"maxTokens": _THINKING_BUDGET + 512, "temperature": 1},
            additionalModelRequestFields={
                "thinking": {"type": "enabled", "budget_tokens": _THINKING_BUDGET}
            },
        )
        return self._parse_response(resp, fallback_score, used_thinking=True)

    def _parse_response(self, resp: dict, fallback_score: float, used_thinking: bool) -> dict:
        content = resp.get("output", {}).get("message", {}).get("content", [])
        # Find the text block (skip thinking blocks)
        text = ""
        for block in content:
            if block.get("type") == "text" or "text" in block:
                text = block.get("text", "").strip()
                break
        if not text:
            return {"score": fallback_score, "reasoning": "", "red_flag": False}
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
        items = json.loads(text)["results"]
        result = items[0] if items else {}
        result["used_thinking"] = used_thinking
        return result

    def refresh_memory(self, memory: Optional[AgentMemory] = None) -> None:
        if memory is not None:
            self._memory = memory
