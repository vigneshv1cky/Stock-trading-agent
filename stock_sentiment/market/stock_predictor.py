"""Predicts stock movement by combining news sentiment with technical archetypes."""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

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

class StockPredictor:
    """Combines sentiment, technicals, and volume with archetype scoring."""

    def predict(self, stock, articles, technicals) -> StockPrediction:
        symbol = stock.symbol
        print(f"[StockPredictor] Scoring {symbol} ({stock.archetype}) | RVOL: {stock.volume_ratio:.2f}")

        # --- 1. Sentiment Analysis ---
        avg_sentiment = 0.0
        bullish_count = 0
        bearish_count = 0
        top_headlines = []
        if articles:
            scores = [a.normalized_score for a in articles]
            avg_sentiment = sum(scores) / len(scores)
            bullish_count = sum(1 for s in scores if s > 0.2)
            bearish_count = sum(1 for s in scores if s < -0.2)
            sorted_articles = sorted(articles, key=lambda x: abs(x.normalized_score), reverse=True)
            for a in sorted_articles[:3]:
                top_headlines.append((a.article.title, a.normalized_score, a.article.source, a.article.url))
        
        sent_score = (avg_sentiment + 1) * 50
        if bullish_count >= 3: sent_score += 15
        sent_score = min(100.0, sent_score)

        # --- 2. Volume Bonus ---
        vol_score = 50.0 + (min(2.0, stock.volume_ratio - 1.0) * 30.0)
        vol_score = max(0.0, min(100.0, vol_score))

        # --- 3. Momentum & Technicals ---
        mom_score = 0.0
        tech_score = 50.0 
        rsi = technicals.rsi_14 if technicals else 50.0

        if stock.archetype == "MOMENTUM":
            mom_score = min(100.0, stock.change_3m_pct * 1.5)
            tech_score = 70.0 if rsi < 70 else 40.0
        elif stock.archetype == "BREAKOUT":
            mom_score = min(100.0, (stock.change_1w_pct * 4) + (stock.change_1m_pct * 1))
            tech_score = 90.0 if stock.volume_ratio > 2.0 else 60.0
        elif stock.archetype == "RECOVERY":
            mom_score = 60.0 + min(40.0, stock.change_1w_pct * 5)
            if rsi < 35: tech_score = 95.0
            elif rsi < 45: tech_score = 80.0
            else: tech_score = 50.0

        overall = (mom_score * 0.30) + (vol_score * 0.20) + (tech_score * 0.25) + (sent_score * 0.25)
        
        # --- 4. Logic & Rating ---
        rating = "NEUTRAL"
        if overall >= 60: rating = "BULLISH"
        if overall <= 40: rating = "BEARISH"
        reasoning = [f"Archetype: {stock.archetype}", f"RVOL: {stock.volume_ratio:.1f}x"]
        if stock.days_to_earnings is not None:
            reasoning.append(f"Earnings in {stock.days_to_earnings} days")

        # Range and Chart Data
        low_3m = min(stock.daily_closes_3m) if stock.daily_closes_3m else stock.current_price
        high_3m = max(stock.daily_closes_3m) if stock.daily_closes_3m else stock.current_price
        sparkline = stock.daily_closes_3m if stock.daily_closes_3m else [stock.current_price]

        return StockPrediction(
            symbol=symbol, current_price=stock.current_price,
            change_3m_pct=stock.change_3m_pct, change_1m_pct=stock.change_1m_pct, change_1w_pct=stock.change_1w_pct,
            low_3m=low_3m, high_3m=high_3m, sparkline_3m=sparkline,
            archetype=stock.archetype, prediction=rating, confidence=overall, overall_score=overall,
            momentum_score=mom_score, sentiment_score=sent_score, technical_score=tech_score, volume_score=vol_score,
            volume_ratio=stock.volume_ratio,
            avg_sentiment=avg_sentiment, bullish_count=bullish_count, bearish_count=bearish_count,
            top_headlines=top_headlines, rsi=rsi, 
            days_to_earnings=stock.days_to_earnings,
            predicted_move="+5-12% (Oversold Bounce)" if rating == "BULLISH" and rsi < 35 else "+3-7% (Standard)",
            reasoning=reasoning + [f"Sent: {avg_sentiment:.2f}", f"RSI: {rsi:.0f}"]
        )
