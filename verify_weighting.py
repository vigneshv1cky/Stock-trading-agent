import sys
import os
from datetime import datetime, timedelta, timezone

# Add current directory to path
sys.path.append(os.getcwd())

from stock_sentiment.market.stock_predictor import StockPredictor
from stock_sentiment.market.screener import ScreenedStock
from stock_sentiment.news.base import ScoredArticle, Article

def test_weighting():
    predictor = StockPredictor()
    
    # Mock stock
    stock = ScreenedStock(
        symbol="AAPL",
        current_price=150.0,
        price_3m_ago=120.0,
        change_3m_pct=20.0,
        change_1m_pct=10.0,
        change_1w_pct=5.0,
        high_3m=160.0,
        low_3m=110.0,
        avg_volume=1000000,
        current_volume=1200000,
        daily_closes_3m=[120.0, 150.0]
    )
    
    now = datetime.now(timezone.utc)
    
    # Mock articles with different ages
    # < 12h: weight 1.0
    # 12-24h: weight 0.8
    # 1-3 days: weight 0.5
    # 3-7 days: weight 0.2
    articles = [
        ScoredArticle(
            article=Article("Title 1", "Summary 1", "Source 1", "url1", now - timedelta(hours=5)),
            sentiment_label="positive",
            sentiment_score=0.9,
            normalized_score=1.0
        ),
        ScoredArticle(
            article=Article("Title 2", "Summary 2", "Source 2", "url2", now - timedelta(hours=18)),
            sentiment_label="negative",
            sentiment_score=0.9,
            normalized_score=-1.0
        ),
        ScoredArticle(
            article=Article("Title 3", "Summary 3", "Source 3", "url3", now - timedelta(days=2)),
            sentiment_label="positive",
            sentiment_score=0.9,
            normalized_score=1.0
        ),
        ScoredArticle(
            article=Article("Title 4", "Summary 4", "Source 4", "url4", now - timedelta(days=5)),
            sentiment_label="negative",
            sentiment_score=0.9,
            normalized_score=-1.0
        )
    ]
    
    # Expected weighted average:
    # (1.0 * 1.0) + (0.8 * -1.0) + (0.5 * 1.0) + (0.2 * -1.0)
    # = 1.0 - 0.8 + 0.5 - 0.2 = 0.5
    # Sum of weights: 1.0 + 0.8 + 0.5 + 0.2 = 2.5
    # Weighted avg: 0.5 / 2.5 = 0.2
    
    prediction = predictor.predict(stock, articles, None)
    print(f"Calculated Weighted Avg Sentiment: {prediction.avg_sentiment}")
    
    expected = 0.2
    if abs(prediction.avg_sentiment - expected) < 0.001:
        print("Test Passed!")
    else:
        print(f"Test Failed! Expected {expected}, got {prediction.avg_sentiment}")
        sys.exit(1)

    # Test dictionary input
    dict_articles = [
        {
            "title": "Title 1",
            "source": "Source 1",
            "url": "url1",
            "normalized_score": 1.0,
            "published_at": (now - timedelta(hours=5)).isoformat()
        },
        {
            "title": "Title 2",
            "source": "Source 2",
            "url": "url2",
            "normalized_score": -1.0,
            "published_at": (now - timedelta(hours=18)).isoformat()
        }
    ]
    # Expected: (1.0*1.0 + 0.8*-1.0) / (1.0 + 0.8) = 0.2 / 1.8 = 0.111...
    prediction_dict = predictor.predict(stock, dict_articles, None)
    print(f"Calculated Weighted Avg (Dict): {prediction_dict.avg_sentiment}")
    expected_dict = 0.2 / 1.8
    if abs(prediction_dict.avg_sentiment - expected_dict) < 0.001:
        print("Dict Test Passed!")
    else:
        print(f"Dict Test Failed! Expected {expected_dict}, got {prediction_dict.avg_sentiment}")
        sys.exit(1)

if __name__ == "__main__":
    test_weighting()
