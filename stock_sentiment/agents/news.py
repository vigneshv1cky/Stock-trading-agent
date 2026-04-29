"""NewsAgent — fetches news via Polygon.io (falls back to Google RSS) and scores sentiment.

Subscribes to: symbol.screened
Publishes to:  symbol.analysed
"""

import asyncio
import os
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import feedparser  # type: ignore[import-untyped]
    _HAS_FEEDPARSER = True
except ImportError:
    _HAS_FEEDPARSER = False

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
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE
        self._polygon = None

    def _get_polygon(self):
        if self._polygon is None and _POLYGON_KEY:
            try:
                from polygon import RESTClient
                self._polygon = RESTClient(api_key=_POLYGON_KEY)
            except ImportError:
                self.log.warning("polygon-api-client not installed; falling back to RSS")
        return self._polygon

    async def run(self) -> None:
        source = "Polygon.io" if _POLYGON_KEY else "Google RSS"
        self.log.info("NewsAgent using %s for news", source)
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
    # Fetch — Polygon.io primary, Google RSS fallback
    # ------------------------------------------------------------------

    def _fetch_news(self, sym: str) -> list[Article]:
        search_sym = sym.split("/")[0] if "/" in sym else sym  # BTC/USD → BTC
        articles = self._fetch_polygon(search_sym) if _POLYGON_KEY else []
        if not articles:
            articles = self._fetch_rss(search_sym)
        return articles

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
            self.log.warning("Polygon news failed for %s: %s — falling back to RSS", sym, exc)
            return []

    def _fetch_rss(self, sym: str) -> list[Article]:
        if not _HAS_FEEDPARSER:
            return []
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=_LOOKBACK_DAYS)
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
            for entry in feed.entries[:_MAX_ARTICLES]:
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
            time.sleep(0.1)
        except Exception:
            pass
        return articles

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
