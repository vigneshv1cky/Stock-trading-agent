"""NewsAgent — fetches news via Polygon.io and scores sentiment with Haiku.

Subscribes to: symbol.screened
Publishes to:  symbol.analysed
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

from stock_sentiment.news.base import Article, ScoredArticle
from stock_sentiment.nlp.sentiment import SentimentAnalyzer

from .base import BaseAgent
from .event_bus import EventBus

_POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "")
_MAX_ARTICLES = 10
_LOOKBACK_DAYS = 7


class NewsAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "NewsAgent")
        self._queue = bus.subscribe("symbol.screened")
        self._sentiment = SentimentAnalyzer()
        self._polygon = None

    def _get_polygon(self):
        if self._polygon is None and _POLYGON_KEY:
            try:
                from polygon import RESTClient
                self._polygon = RESTClient(api_key=_POLYGON_KEY)
            except ImportError:
                self.log.warning("polygon-api-client not installed")
        return self._polygon

    async def run(self) -> None:
        source = "Polygon.io" if _POLYGON_KEY else "none (no POLYGON_API_KEY)"
        self.log.info("NewsAgent using %s for news", source)
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, data: dict) -> None:
        sym = data["symbol"]
        stock = data["screened_stock"]
        loop = asyncio.get_running_loop()
        try:
            articles = await loop.run_in_executor(None, self._fetch_news, sym)
            scored = await loop.run_in_executor(None, self._score_articles, articles)
            self.log.info("News: %s  %d articles → %d scored", sym, len(articles), len(scored))
            await self.bus.publish("symbol.analysed", {
                "symbol": sym,
                "screened_stock": stock,
                "scored_articles": scored,
            })
        except Exception as exc:
            self.log.error("News error %s: %s", sym, exc)

    # ------------------------------------------------------------------
    # Fetch — Polygon.io only
    # ------------------------------------------------------------------

    def _fetch_news(self, sym: str) -> list[Article]:
        return self._fetch_polygon(sym) if _POLYGON_KEY else []

    def _fetch_polygon(self, sym: str) -> list[Article]:
        client = self._get_polygon()
        if not client:
            return []
        try:
            now = datetime.now(timezone.utc)
            since = (now - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            articles: list[Article] = []
            for item in client.list_ticker_news(
                ticker=sym,
                published_utc_gte=since,
                order="desc",
                limit=_MAX_ARTICLES,
            ):
                published_at = now
                if hasattr(item, "published_utc") and item.published_utc:
                    try:
                        published_at = datetime.fromisoformat(
                            item.published_utc.replace("Z", "+00:00")
                        )
                    except Exception:
                        pass

                title = getattr(item, "title", "") or ""
                description = getattr(item, "description", "") or ""
                publisher = getattr(item, "publisher", None)
                source = (
                    publisher.name
                    if publisher and hasattr(publisher, "name")
                    else "Polygon"
                )
                url = getattr(item, "article_url", "") or ""

                if not title:
                    continue
                articles.append(Article(
                    title=title,
                    summary=description,
                    source=source,
                    url=url,
                    published_at=published_at,
                ))
            return articles
        except Exception as exc:
            self.log.warning("Polygon news failed for %s: %s", sym, exc)
            return []

    # ------------------------------------------------------------------
    # Sentiment scoring via Bedrock
    # ------------------------------------------------------------------

    def _score_articles(self, articles: list[Article]) -> list[ScoredArticle]:
        if not articles:
            return []
        texts = [a.raw_text for a in articles]
        results = self._sentiment.analyze_batch(texts)
        return [
            ScoredArticle(
                article=art,
                sentiment_label=res.label,
                sentiment_score=res.score,
                normalized_score=res.normalized,
            )
            for art, res in zip(articles, results)
        ]
