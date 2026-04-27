import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.append(os.getcwd())

from stock_sentiment.market.stock_predictor import StockPredictor, _recency_weight, _source_weight
from stock_sentiment.market.screener import ScreenedStock
from stock_sentiment.news.base import ScoredArticle, Article


def test_weighting():
    predictor = StockPredictor()

    stock = ScreenedStock(
        symbol="AAPL",
        current_price=150.0,
        change_3m_pct=20.0,
        change_1m_pct=10.0,
        change_1w_pct=5.0,
        avg_volume=1_000_000,
        volume_ratio=1.2,
        archetype="MOMENTUM",
        daily_closes_3m=[120.0, 135.0, 150.0],
    )

    now = datetime.now(timezone.utc)
    articles = [
        ScoredArticle(
            article=Article("Title 1", "Summary 1", "Source 1", "url1", now - timedelta(hours=5)),
            sentiment_label="positive", sentiment_score=0.9, normalized_score=1.0,
        ),
        ScoredArticle(
            article=Article("Title 2", "Summary 2", "Source 2", "url2", now - timedelta(hours=18)),
            sentiment_label="negative", sentiment_score=0.9, normalized_score=-1.0,
        ),
        ScoredArticle(
            article=Article("Title 3", "Summary 3", "Source 3", "url3", now - timedelta(days=2)),
            sentiment_label="positive", sentiment_score=0.9, normalized_score=1.0,
        ),
        ScoredArticle(
            article=Article("Title 4", "Summary 4", "Source 4", "url4", now - timedelta(days=5)),
            sentiment_label="negative", sentiment_score=0.9, normalized_score=-1.0,
        ),
    ]

    prediction = predictor.predict(stock, articles, ti=None)

    # avg_sentiment: recency-decay × source-quality weighted average.
    # Ages: 5h (+1.0), 18h (-1.0), 48h (+1.0), 120h (-1.0); all "Source N" → default weight.
    # Recent bullish (5h) outweighs old bearish (120h) → result should be positive.
    expected_weights = [
        _recency_weight(a.article.published_at) * _source_weight(a.article.source)
        for a in articles
    ]
    total_w = sum(expected_weights)
    expected_avg = sum(a.normalized_score * w for a, w in zip(articles, expected_weights)) / total_w
    _assert("avg_sentiment (recency+source weighted)", prediction.avg_sentiment, expected_avg, tol=1e-6)
    if prediction.avg_sentiment <= 0:
        print("  ✗ FAILED  avg_sentiment should be positive (recent bullish > old bearish)")
        sys.exit(1)

    # bullish_count: articles with normalized_score > 0.2 → 2 of 4
    _assert("bullish_count", prediction.bullish_count, 2)

    # sent_score = (avg_sentiment + 1) * 50 + bonus(0, bullish_count<3)
    expected_sent = min(100.0, (expected_avg + 1) * 50)
    _assert("sentiment_score", prediction.sentiment_score, expected_sent, tol=0.01)

    # vol_score = 50 + min(2.0, rvol - 1.0) * 30 = 50 + 0.2 * 30 = 56.0
    _assert("volume_score", prediction.volume_score, 56.0)

    # momentum_score for MOMENTUM: min(100, change_3m * 1.5) = min(100, 30) = 30.0
    _assert("momentum_score (MOMENTUM archetype)", prediction.momentum_score, 30.0)

    # technical_score for MOMENTUM: 70 if rsi < 70 (rsi defaults to 50 when ti=None) → 70.0
    _assert("technical_score (MOMENTUM, RSI=50)", prediction.technical_score, 70.0)

    print("\nAll tests passed!")


def _assert(label: str, got, expected, tol: float = 0.001):
    if isinstance(expected, float):
        ok = abs(got - expected) < tol
    else:
        ok = got == expected
    status = "✓" if ok else "✗ FAILED"
    print(f"  {status}  {label}: got {got!r}, expected {expected!r}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    test_weighting()
