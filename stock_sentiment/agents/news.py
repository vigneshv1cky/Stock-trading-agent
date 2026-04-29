"""NewsAgent — fetches last-7-day articles for a screened symbol and scores sentiment.

Subscribes to: symbol.screened
Publishes to:  symbol.analysed
"""

import asyncio
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import feedparser  # type: ignore[import-untyped]

from stock_sentiment.news.base import Article, ScoredArticle
from stock_sentiment.nlp.sentiment import SentimentAnalyzer

from .base import BaseAgent
from .event_bus import EventBus


class NewsAgent(BaseAgent):
    def __init__(self, bus: EventBus):
        super().__init__(bus, "NewsAgent")
        self._queue = bus.subscribe("symbol.screened")
        self._sentiment = SentimentAnalyzer()
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    async def run(self) -> None:
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, data: dict) -> None:
        sym = data["symbol"]
        stock = data["screened_stock"]
        loop = asyncio.get_event_loop()
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
    # Blocking helpers — run in executor
    # ------------------------------------------------------------------

    def _fetch_news(self, sym: str) -> list[Article]:
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_ctx)
        )
        query = (
            f"{sym} stock"
            f" after:{week_ago.strftime('%Y-%m-%d')}"
            f" before:{now.strftime('%Y-%m-%d')}"
        )
        url = (
            "https://news.google.com/rss/search"
            f"?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        )
        articles: list[Article] = []
        try:
            response = opener.open(url, timeout=10)
            feed = feedparser.parse(response.read())
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                pub = entry.get("published_parsed")
                published_at = (
                    datetime(
                        pub.tm_year, pub.tm_mon, pub.tm_mday,
                        pub.tm_hour, pub.tm_min, pub.tm_sec,
                        tzinfo=timezone.utc,
                    )
                    if pub else now
                )
                articles.append(Article(
                    title=title,
                    summary=entry.get("summary", ""),
                    source=entry.get("source", {}).get("title", "Unknown"),
                    url=entry.get("link", ""),
                    published_at=published_at,
                ))
            time.sleep(0.1)  # gentle throttle
        except Exception:
            pass
        return articles

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
