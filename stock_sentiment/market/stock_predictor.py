"""Predicts stock movement by combining sub-scores with a single Bedrock LLM call (Option A)."""

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

import boto3

from stock_sentiment.market.weight_optimizer import load_weights

_RECENCY_HALF_LIFE_H = 8.0    # score halves every 8 hours — today's news dominates

_COUNT_WINDOW_H = 24.0        # only count bullish/bearish articles from last 24h


def _source_weight(_: str) -> float:
    return 1.0


def _recency_weight(published_at: datetime) -> float:
    age_h = max(0.0, (datetime.now(_ET) - published_at).total_seconds() / 3600)
    return math.exp(-age_h * math.log(2) / _RECENCY_HALF_LIFE_H)

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_SYSTEM_PROMPT = (
    "You are a qualitative news analyst for equity swing trading. "
    "For each numbered stock, headlines are listed newest-first with age labels. "
    "CRITICAL: Prioritise headlines from the last 4 hours above all others. "
    "If today's breaking news contradicts older bullish momentum, override with today's signal. "
    "Identify: (1) Catalysts — earnings beat, contract win, product launch, analyst upgrade, M&A. "
    "(2) Red flags — SEC probe, lawsuit, guidance cut, CEO departure, recall, bankruptcy risk. "
    "(3) SECOND-ORDER RISKS — reason through supplier/customer/sector chains even when the stock's "
    "own headline looks positive. Examples of connections to apply: "
    "AI customer misses revenue or cuts capex (OpenAI, Microsoft, Google) → GPU/chip suppliers bearish (NVDA, AMD, ARM, AVGO, INTC). "
    "Hyperscaler slows data centre build-out → server/networking hardware bearish (DELL, HPE, SMCI, ANET, CSCO). "
    "EV demand miss or automaker cuts production → battery/cell bearish (ENVX, WOLF, QS), power semi bearish (ON, WOLF). "
    "Smartphone shipments disappoint → display/memory/AP suppliers bearish (MU, QCOM, SWKS, QRVO). "
    "Oil price spike or refinery outage → airlines and logistics bearish (DAL, UAL, UPS, FDX). "
    "Rising interest rates or credit-tightening news → homebuilders and REITs bearish (LEN, DHI, NVR). "
    "Retail sales miss or consumer confidence drop → discretionary and ad-spend bearish (META, SNAP, PINS, ETSY). "
    "Biotech FDA rejection → sector sentiment bearish for clinical-stage peers in the same indication. "
    "Flag the affected stock BEARISH for second-order risk even if its own headline looks neutral or positive. "
    "Archetypes: FRESH_BREAKOUT=big intraday move just starting (weight today's news heavily), "
    "BREAKOUT=multi-day surge, MOMENTUM=sustained trend, RECOVERY=bouncing from drawdown. "
    "Score 0-100: 50=neutral/no news, >60=positive catalyst, <40=negative risk. "
    "BULLISH≥60 | BEARISH≤40 | NEUTRAL otherwise. "
    'Return ONLY valid JSON: {"results": [{"score": 65, "rating": "BULLISH", "red_flag": false, "reasoning": "one sentence"}]}'
)


@dataclass
class StockPrediction:
    symbol: str
    current_price: float
    change_3m_pct: float
    change_1m_pct: float
    change_1w_pct: float
    low_3m: float
    high_3m: float
    sparkline_3m: list
    archetype: str
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


class StockPredictor:
    """Combines sentiment, technicals, and volume with learned weights, then calls
    Claude (Bedrock) once for all stocks to get holistic conviction scores."""

    def __init__(self):
        self._weights = load_weights()
        self._bedrock = None
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    def _get_bedrock(self):
        if self._bedrock is None:
            self._bedrock = boto3.client("bedrock-runtime", region_name=self._region)
        return self._bedrock

    def predict_all(self, stocks, scored_articles_map: dict, technicals_map: dict) -> list[StockPrediction]:
        """Score all stocks in one Bedrock call. Main entry point for the screener pipeline."""
        partials = [
            self._compute_sub_scores(stock, scored_articles_map.get(stock.symbol, []), technicals_map.get(stock.symbol))
            for stock in stocks
        ]
        llm_results = self._llm_score_batch(stocks, partials, scored_articles_map)

        predictions = []
        for stock, sub, llm in zip(stocks, partials, llm_results):
            formula_score = sub["formula_score"]
            llm_qual = float(llm.get("score", formula_score))
            red_flag = bool(llm.get("red_flag", False))

            # 70% quantitative formula + 30% qualitative LLM news assessment
            score = formula_score * 0.50 + llm_qual * 0.50

            # Hard red-flag override: bad news caps conviction below BEARISH threshold
            if red_flag:
                score = min(score, 35.0)
                print(f"[StockPredictor] RED FLAG {stock.symbol}: {llm.get('reasoning', '')}")

            if score >= 60:
                rating = "BULLISH"
            elif score <= 40:
                rating = "BEARISH"
            else:
                rating = "NEUTRAL"

            reasoning = [f"Archetype: {stock.archetype}", f"RVOL: {stock.volume_ratio:.1f}x"]
            if stock.days_to_earnings is not None:
                reasoning.append(f"Earnings in {stock.days_to_earnings} days")
            if llm.get("reasoning"):
                reasoning.append(llm["reasoning"])
            reasoning.append(f"Sent: {sub['avg_sentiment']:.2f} | RSI: {sub['rsi']:.0f}")
            if red_flag:
                reasoning.append("RED FLAG: negative news override applied")

            predictions.append(self._build_prediction(stock, sub, score, rating, reasoning))

        return predictions

    def predict(self, stock, articles, ti) -> StockPrediction:
        """Single-stock formula-only prediction (fallback, no LLM call)."""
        sub = self._compute_sub_scores(stock, articles, ti)
        score = sub["formula_score"]
        rating = "BULLISH" if score >= 60 else ("BEARISH" if score <= 40 else "NEUTRAL")
        reasoning = [
            f"Archetype: {stock.archetype}", f"RVOL: {stock.volume_ratio:.1f}x",
            f"Sent: {sub['avg_sentiment']:.2f}", f"RSI: {sub['rsi']:.0f}",
        ]
        if stock.days_to_earnings is not None:
            reasoning.append(f"Earnings in {stock.days_to_earnings} days")
        return self._build_prediction(stock, sub, score, rating, reasoning)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_sub_scores(self, stock, articles, ti) -> dict:
        """Compute all sub-scores using current (possibly learned) weights."""
        # --- Sentiment (recency-decay × source-quality weighted average) ---
        avg_sentiment = 0.0
        bullish_count = 0
        bearish_count = 0
        top_headlines = []
        if articles:
            weights = [
                _recency_weight(a.article.published_at) * _source_weight(a.article.source)
                for a in articles
            ]
            total_w = sum(weights)
            avg_sentiment = (
                sum(a.normalized_score * w for a, w in zip(articles, weights)) / total_w
                if total_w > 0 else 0.0
            )
            count_cutoff = datetime.now(_ET) - timedelta(hours=_COUNT_WINDOW_H)
            bullish_count = sum(1 for a in articles if a.normalized_score > 0.2 and a.article.published_at >= count_cutoff)
            bearish_count = sum(1 for a in articles if a.normalized_score < -0.2 and a.article.published_at >= count_cutoff)
            # Rank headlines by recency first so the dashboard surfaces today's news
            ranked = sorted(
                zip(articles, weights),
                key=lambda aw: aw[0].article.published_at,
                reverse=True,
            )
            top_headlines = [
                (a.article.title, a.normalized_score, a.article.source, a.article.url)
                for a, _ in ranked[:5]
            ]

        sent_score = min(100.0, (avg_sentiment + 1) * 50 + (15 if bullish_count >= 3 else 0))

        # --- Volume ---
        vol_score = max(0.0, min(100.0, 50.0 + min(2.0, stock.volume_ratio - 1.0) * 30.0))

        # --- Momentum + Technicals (archetype-aware) ---
        rsi = ti.rsi_14 if ti else 50.0

        if stock.archetype == "FRESH_BREAKOUT":
            # Today's intraday move IS the catalyst — score by magnitude of the day's move
            # 5% today → 75 mom_score; intraday_adj below adds ±50 on top
            mom_score = min(100.0, abs(stock.change_today_pct) * 15)
            tech_score = 90.0 if stock.volume_ratio >= 3.0 else 70.0
        elif stock.archetype == "MOMENTUM":
            mom_score = min(100.0, stock.change_3m_pct * 1.5)
            tech_score = 70.0 if rsi < 70 else 40.0
        elif stock.archetype == "BREAKOUT":
            mom_score = min(100.0, (stock.change_1w_pct * 4) + stock.change_1m_pct)
            tech_score = 90.0 if stock.volume_ratio > 2.0 else 60.0
        elif stock.archetype == "RECOVERY":
            mom_score = 60.0 + min(40.0, stock.change_1w_pct * 5)
            tech_score = 95.0 if rsi < 35 else (80.0 if rsi < 45 else 50.0)
        else:
            mom_score = 0.0
            tech_score = 50.0

        w = self._weights.get(stock.archetype, self._weights.get("default", [0.30, 0.20, 0.25, 0.25]))
        formula_score = (mom_score * w[0]) + (vol_score * w[1]) + (tech_score * w[2]) + (sent_score * w[3])

        # Intraday momentum modifier — makes today's price action visible to the formula.
        # Multi-week returns bury a 2% single-day drop; this surfaces it directly.
        # Symmetric quadratic: -2% → -32pts, +2% → +32pts, ±2.5%+ → ±50pts (capped)
        today = stock.change_today_pct
        if today < 0:
            intraday_adj = -min(50.0, abs(today) ** 2 * 8)
        else:
            intraday_adj = min(50.0, today ** 2 * 8)
        formula_score = max(0.0, min(100.0, formula_score + intraday_adj))

        return {
            "mom_score": mom_score, "vol_score": vol_score, "tech_score": tech_score,
            "sent_score": sent_score, "formula_score": formula_score, "rsi": rsi,
            "avg_sentiment": avg_sentiment, "bullish_count": bullish_count, "bearish_count": bearish_count,
            "top_headlines": top_headlines,
        }

    def _llm_score_batch(self, stocks, partials: list[dict], scored_articles_map: dict) -> list[dict]:
        """Send all stocks to Claude in a single Bedrock call for qualitative news assessment."""
        now = datetime.now(_ET)

        # Collect bullish and bearish signals from the last 4 hours across all stocks
        macro_cutoff = now - timedelta(hours=4)
        macro_bullish: list[tuple[datetime, str, str]] = []
        macro_bearish: list[tuple[datetime, str, str]] = []
        seen_titles: set[str] = set()
        for stock in stocks:
            for a in scored_articles_map.get(stock.symbol, []):
                if a.article.published_at < macro_cutoff:
                    continue
                title = a.article.title[:120]
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                if a.normalized_score > 0.15:
                    macro_bullish.append((a.article.published_at, title, a.article.source))
                elif a.normalized_score < -0.15:
                    macro_bearish.append((a.article.published_at, title, a.article.source))
        macro_bullish.sort(key=lambda x: x[0], reverse=True)
        macro_bearish.sort(key=lambda x: x[0], reverse=True)

        macro_block = ""
        if macro_bullish or macro_bearish:
            sections = []
            if macro_bullish:
                lines_bull = [f'  + "{t}" ({src})' for _, t, src in macro_bullish[:6]]
                sections.append("BULLISH:\n" + "\n".join(lines_bull))
            if macro_bearish:
                lines_bear = [f'  - "{t}" ({src})' for _, t, src in macro_bearish[:6]]
                sections.append("BEARISH:\n" + "\n".join(lines_bear))
            macro_block = (
                "TODAY'S MACRO SIGNALS (last 4h across all tracked stocks):\n"
                + "\n".join(sections)
                + "\n\nApply this sector context when scoring each stock below — "
                "catalysts and risks affecting one name ripple to suppliers, customers, and peers.\n\n"
            )

        lines = []
        for i, stock in enumerate(stocks, 1):
            # Sort this stock's articles newest-first and label with age
            articles = sorted(
                scored_articles_map.get(stock.symbol, []),
                key=lambda a: a.article.published_at,
                reverse=True,
            )[:5]

            if articles:
                headline_parts = []
                for a in articles:
                    age_h = (now - a.article.published_at).total_seconds() / 3600
                    age_str = f"{age_h * 60:.0f}m ago" if age_h < 1 else f"{age_h:.0f}h ago"
                    headline_parts.append(f'[{age_str}] "{a.article.title[:180]}" ({a.article.source})')
                headlines_str = "\n     ".join(headline_parts)
            else:
                headlines_str = "No recent news"

            earnings = (
                f" | Earnings in {stock.days_to_earnings}d"
                if stock.days_to_earnings is not None and stock.days_to_earnings <= 14
                else ""
            )
            lines.append(
                f"{i}. {stock.symbol} | {stock.archetype}{earnings}"
                f"\n   Headlines (newest first):\n     {headlines_str}"
            )

        now_str = now.strftime("%Y-%m-%d %H:%M ET")
        user_text = f"Current time: {now_str}\n\n{macro_block}" + "\n\n".join(lines)

        print(f"[StockPredictor] Calling Claude Haiku 4.5 (Bedrock Converse) for {len(stocks)} stocks...")
        try:
            resp = self._get_bedrock().converse(
                modelId=_MODEL_ID,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                inferenceConfig={"maxTokens": 4096, "temperature": 0},
            )
            content = resp.get("output", {}).get("message", {}).get("content", [])
            text = content[0]["text"].strip() if content else ""
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
            items = json.loads(text)["results"]
            while len(items) < len(stocks):
                items.append({})
            print(f"[StockPredictor] Haiku scoring complete: {len(items)} results received.")
            return items[: len(stocks)]
        except Exception as e:
            print(f"[StockPredictor] LLM scoring failed ({e}), falling back to formula scores.")
            return [{"score": sub["formula_score"], "rating": "", "reasoning": "", "red_flag": False} for sub in partials]

    def _build_prediction(self, stock, sub: dict, score: float, rating: str, reasoning: list[str]) -> StockPrediction:
        low_3m = min(stock.daily_closes_3m) if stock.daily_closes_3m else stock.current_price
        high_3m = max(stock.daily_closes_3m) if stock.daily_closes_3m else stock.current_price
        return StockPrediction(
            symbol=stock.symbol, current_price=stock.current_price,
            change_3m_pct=stock.change_3m_pct, change_1m_pct=stock.change_1m_pct, change_1w_pct=stock.change_1w_pct,
            low_3m=low_3m, high_3m=high_3m, sparkline_3m=stock.daily_closes_3m or [stock.current_price],
            archetype=stock.archetype, prediction=rating, confidence=score, overall_score=score,
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
