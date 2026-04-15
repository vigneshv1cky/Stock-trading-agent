"""Predicts stock movement by combining this week's news sentiment with technicals."""

import math
from dataclasses import dataclass
from typing import Optional

from etf_sentiment.market.screener import ScreenedStock
from etf_sentiment.market.technicals import TechnicalIndicators


@dataclass
class StockPrediction:
    """Full prediction for a screened stock."""
    symbol: str
    current_price: float

    # 3-month performance
    change_3m_pct: float
    change_1m_pct: float
    change_1w_pct: float
    high_3m: float
    low_3m: float
    sparkline_3m: list  # daily closes

    # This week's news
    article_count: int
    avg_sentiment: float  # -1 to +1
    bullish_count: int
    bearish_count: int
    neutral_count: int
    top_headlines: list  # [(title, score, source)]

    # Technicals
    rsi: Optional[float]
    macd_crossover: str
    trend_direction: str
    trend_strength: float
    volume_ratio: Optional[float]
    price_vs_sma20: str

    # Prediction
    prediction: str  # "BULLISH", "BEARISH", "NEUTRAL"
    confidence: float  # 0-100
    predicted_move: str  # e.g. "+2-5%", "-1-3%", "sideways"
    reasoning: list  # List of reasoning strings

    # Composite score
    momentum_score: float  # 0-100 based on 3m/1m/1w returns
    sentiment_score: float  # 0-100 based on news
    technical_score: float  # 0-100 based on indicators
    overall_score: float  # weighted combination


class StockPredictor:
    """Predicts stock movement from screened stocks + news + technicals."""

    def predict(
        self,
        stock: ScreenedStock,
        articles: list,  # ScoredArticle or dicts
        technicals: Optional[TechnicalIndicators],
    ) -> StockPrediction:
        """Generate a prediction for a single stock."""

        # --- Parse articles ---
        sentiments = []
        headlines = []
        bullish = bearish = neutral = 0

        for a in articles:
            if hasattr(a, "normalized_score"):
                score = a.normalized_score
                title = a.article.title
                source = a.article.source
            else:
                score = a.get("normalized_score", 0.0)
                title = a.get("title", "")
                source = a.get("source", "")

            sentiments.append(score)
            headlines.append((title, score, source))

            if score > 0.1:
                bullish += 1
            elif score < -0.1:
                bearish += 1
            else:
                neutral += 1

        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
        # Sort headlines by absolute sentiment (most impactful first)
        headlines.sort(key=lambda x: abs(x[1]), reverse=True)

        # --- Momentum score (0-100) ---
        # Rewards consistent gains across timeframes
        momentum = self._compute_momentum_score(stock)

        # --- Sentiment score (0-100) ---
        sent_score = self._compute_sentiment_score(
            avg_sentiment, len(articles), bullish, bearish
        )

        # --- Technical score (0-100) ---
        tech_score = self._compute_technical_score(technicals)

        # --- Overall score: momentum 40% + sentiment 30% + technical 30% ---
        overall = momentum * 0.40 + sent_score * 0.30 + tech_score * 0.30

        # --- Prediction ---
        prediction, confidence, predicted_move = self._classify(
            overall, avg_sentiment, stock, technicals
        )

        # --- Reasoning ---
        reasoning = self._build_reasoning(
            stock, avg_sentiment, bullish, bearish, technicals, momentum
        )

        return StockPrediction(
            symbol=stock.symbol,
            current_price=stock.current_price,
            change_3m_pct=stock.change_3m_pct,
            change_1m_pct=stock.change_1m_pct,
            change_1w_pct=stock.change_1w_pct,
            high_3m=stock.high_3m,
            low_3m=stock.low_3m,
            sparkline_3m=stock.daily_closes_3m,
            article_count=len(articles),
            avg_sentiment=avg_sentiment,
            bullish_count=bullish,
            bearish_count=bearish,
            neutral_count=neutral,
            top_headlines=headlines[:5],
            rsi=technicals.rsi_14 if technicals else None,
            macd_crossover=technicals.macd_crossover if technicals else "none",
            trend_direction=technicals.trend_direction if technicals else "unknown",
            trend_strength=technicals.trend_strength if technicals else 0,
            volume_ratio=technicals.volume_ratio if technicals else None,
            price_vs_sma20=technicals.price_vs_sma20 if technicals else "unknown",
            prediction=prediction,
            confidence=confidence,
            predicted_move=predicted_move,
            reasoning=reasoning,
            momentum_score=momentum,
            sentiment_score=sent_score,
            technical_score=tech_score,
            overall_score=overall,
        )

    def _compute_momentum_score(self, stock: ScreenedStock) -> float:
        """Score 0-100 based on multi-timeframe returns."""
        score = 0.0

        # 3-month return contribution (0-40)
        if stock.change_3m_pct > 50:
            score += 40
        elif stock.change_3m_pct > 30:
            score += 35
        elif stock.change_3m_pct > 20:
            score += 28
        elif stock.change_3m_pct > 10:
            score += 20
        else:
            score += 10

        # 1-month return contribution (0-35)
        if stock.change_1m_pct > 20:
            score += 35
        elif stock.change_1m_pct > 10:
            score += 28
        elif stock.change_1m_pct > 5:
            score += 20
        elif stock.change_1m_pct > 0:
            score += 12
        else:
            score += 5  # Negative month but positive 3m — pullback

        # 1-week return (0-25) — recent momentum matters most
        if stock.change_1w_pct > 10:
            score += 25
        elif stock.change_1w_pct > 5:
            score += 22
        elif stock.change_1w_pct > 2:
            score += 18
        elif stock.change_1w_pct > 0:
            score += 12
        elif stock.change_1w_pct > -3:
            score += 8  # Mild pullback, could be buying opportunity
        else:
            score += 3  # Sharp pullback

        return min(100, score)

    def _compute_sentiment_score(
        self, avg: float, count: int, bullish: int, bearish: int
    ) -> float:
        """Score 0-100 based on news sentiment."""
        if count == 0:
            return 50.0  # No news = neutral

        # Base: map -1..+1 to 0..100
        base = (avg + 1) / 2 * 100

        # Consensus bonus: if overwhelmingly one direction
        total = bullish + bearish
        if total > 0:
            consensus = max(bullish, bearish) / total
            if consensus > 0.8:
                # Strong consensus — boost toward the direction
                base = base * 0.8 + (100 if bullish > bearish else 0) * 0.2

        # Volume of coverage bonus (more articles = more reliable)
        coverage_factor = min(1.0, count / 5)
        # Pull toward 50 if few articles
        base = base * coverage_factor + 50 * (1 - coverage_factor)

        return max(0, min(100, base))

    def _compute_technical_score(
        self, ti: Optional[TechnicalIndicators]
    ) -> float:
        """Score 0-100 from technical indicators."""
        if not ti or ti.rsi_14 is None:
            return 50.0

        score = 0.0

        # RSI (0-30)
        if ti.rsi_14 < 30:
            score += 30  # Oversold = bounce potential
        elif ti.rsi_14 < 45:
            score += 25
        elif ti.rsi_14 < 60:
            score += 20  # Healthy range
        elif ti.rsi_14 < 70:
            score += 15
        else:
            score += 5  # Overbought = risk

        # MACD (0-25)
        if ti.macd_crossover == "bullish":
            score += 25
        elif ti.macd_histogram and ti.macd_histogram > 0:
            score += 18
        elif ti.macd_crossover == "bearish":
            score += 5
        else:
            score += 12

        # Trend (0-25)
        if ti.trend_direction == "up":
            score += 15 + ti.trend_strength * 10
        elif ti.trend_direction == "sideways":
            score += 12
        else:
            score += 5

        # Price vs MA (0-20)
        if ti.price_vs_sma20 == "above":
            score += 20
        else:
            score += 8

        return min(100, score)

    def _classify(
        self, overall: float, sentiment: float,
        stock: ScreenedStock, ti: Optional[TechnicalIndicators]
    ) -> tuple[str, float, str]:
        """Classify into prediction, confidence, and predicted move range."""

        if overall >= 72:
            prediction = "BULLISH"
            confidence = min(95, 60 + (overall - 72) * 1.5)
            if stock.change_1w_pct > 5:
                predicted_move = "+3-8% (strong momentum continuing)"
            else:
                predicted_move = "+2-5% (breakout potential)"
        elif overall >= 58:
            prediction = "BULLISH"
            confidence = min(75, 45 + (overall - 58) * 1.5)
            predicted_move = "+1-3% (moderate upside)"
        elif overall >= 45:
            prediction = "NEUTRAL"
            confidence = 30 + abs(overall - 50) * 2
            predicted_move = "+/- 1-2% (sideways / consolidation)"
        elif overall >= 35:
            prediction = "BEARISH"
            confidence = min(70, 40 + (45 - overall) * 2)
            predicted_move = "-1-3% (short-term weakness)"
        else:
            prediction = "BEARISH"
            confidence = min(85, 55 + (35 - overall) * 1.5)
            predicted_move = "-3-5%+ (caution advised)"

        # Adjust for overbought/oversold
        if ti and ti.rsi_14 is not None:
            if ti.rsi_14 > 80 and prediction == "BULLISH":
                predicted_move += " [overbought risk]"
                confidence *= 0.85
            elif ti.rsi_14 < 25 and prediction == "BEARISH":
                predicted_move += " [oversold bounce possible]"
                confidence *= 0.85

        return prediction, round(confidence, 1), predicted_move

    def _build_reasoning(
        self, stock, sentiment, bullish, bearish, ti, momentum
    ) -> list[str]:
        reasons = []

        # Momentum
        if stock.change_3m_pct > 30:
            reasons.append(f"Strong 3-month rally (+{stock.change_3m_pct:.1f}%)")
        elif stock.change_3m_pct > 15:
            reasons.append(f"Solid 3-month gains (+{stock.change_3m_pct:.1f}%)")

        if stock.change_1w_pct > 5:
            reasons.append(f"Surging this week (+{stock.change_1w_pct:.1f}%)")
        elif stock.change_1w_pct < -3:
            reasons.append(f"Pulling back this week ({stock.change_1w_pct:.1f}%)")

        # Sentiment
        if bullish + bearish > 0:
            if bullish > bearish * 2:
                reasons.append(f"News overwhelmingly bullish ({bullish}B/{bearish}b)")
            elif bearish > bullish * 2:
                reasons.append(f"News predominantly bearish ({bullish}B/{bearish}b)")
            elif sentiment > 0.2:
                reasons.append("Positive news sentiment this week")
            elif sentiment < -0.2:
                reasons.append("Negative news sentiment this week")

        # Technicals
        if ti:
            if ti.rsi_14 is not None:
                if ti.rsi_14 < 30:
                    reasons.append(f"Oversold (RSI {ti.rsi_14:.0f})")
                elif ti.rsi_14 > 70:
                    reasons.append(f"Overbought (RSI {ti.rsi_14:.0f})")

            if ti.macd_crossover == "bullish":
                reasons.append("MACD bullish crossover")
            elif ti.macd_crossover == "bearish":
                reasons.append("MACD bearish crossover")

            if ti.trend_direction == "up" and ti.trend_strength > 0.6:
                reasons.append("Strong uptrend")

            if ti.volume_ratio and ti.volume_ratio > 1.5:
                reasons.append(f"Volume surge ({ti.volume_ratio:.1f}x avg)")

        if not reasons:
            reasons.append("Mixed signals — proceed with caution")

        return reasons
