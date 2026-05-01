"""NewsAgent — fetches news via Polygon.io and scores sentiment with Haiku.

Subscribes to: symbol.screened
Publishes to:  symbol.analysed
"""

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .base import BaseAgent
from .event_bus import EventBus

_POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "")
_MAX_ARTICLES = 10
_LOOKBACK_HOURS = 1

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_SENTIMENT_SYSTEM = (
    "You are a financial sentiment scorer for equity investors. "
    "Score each numbered headline from -1.0 (very negative) to 1.0 (very positive). "
    "Return ONLY a JSON array of numbers in order, no explanation."
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    summary: str
    source: str
    url: str
    published_at: datetime
    raw_text: str = ""

    def __post_init__(self):
        if not self.raw_text:
            self.raw_text = f"{self.title}. {self.summary}".strip()


@dataclass
class ScoredArticle:
    article: Article
    sentiment_label: str
    sentiment_score: float
    normalized_score: float
    stock_symbols: list = field(default_factory=list)
    relevance_scores: dict = field(default_factory=dict)


@dataclass
class SentimentResult:
    label: str
    score: float
    normalized: float


# ---------------------------------------------------------------------------
# Sentiment helpers
# ---------------------------------------------------------------------------

def _neutral() -> SentimentResult:
    return SentimentResult(label="neutral", score=0.5, normalized=0.0)


def _from_score(s: float) -> SentimentResult:
    s = max(-1.0, min(1.0, float(s)))
    label = "positive" if s > 0.2 else ("negative" if s < -0.2 else "neutral")
    return SentimentResult(label=label, score=abs(s), normalized=s)


def _parse_scores(text_out: str, n: int) -> list[float] | None:
    m = re.search(r'\[[\s\S]*\]', text_out)
    if m:
        try:
            scores = json.loads(m.group())
            if isinstance(scores, list):
                while len(scores) < n:
                    scores.append(0.0)
                return [float(s) for s in scores[:n]]
        except Exception:
            pass
    raw_nums = re.findall(r'-?\d+(?:\.\d+)?', text_out)
    if raw_nums:
        scores = [float(x) for x in raw_nums[:n]]
        while len(scores) < n:
            scores.append(0.0)
        return scores
    return None


# ---------------------------------------------------------------------------
# NewsAgent
# ---------------------------------------------------------------------------

class NewsAgent(BaseAgent):
    _BATCH_SIZE = 50

    def __init__(self, bus: EventBus):
        super().__init__(bus, "NewsAgent")
        self._queue = bus.subscribe("symbol.screened")
        self._polygon = None
        self._region = os.environ.get("AWS_REGION", "us-east-1")

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
            since = (now - timedelta(hours=_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    # Sentiment scoring via Bedrock Haiku
    # ------------------------------------------------------------------

    def _score_articles(self, articles: list[Article]) -> list[ScoredArticle]:
        if not articles:
            return []
        texts = [a.raw_text for a in articles]
        results = self._analyze_batch(texts)
        return [
            ScoredArticle(
                article=art,
                sentiment_label=res.label,
                sentiment_score=res.score,
                normalized_score=res.normalized,
            )
            for art, res in zip(articles, results)
        ]

    def _analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        results: list[SentimentResult] = []
        for batch_start in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[batch_start: batch_start + self._BATCH_SIZE]
            results.extend(self._score_batch(batch, batch_start))
        return results

    def _score_batch(self, texts: list[str], offset: int) -> list[SentimentResult]:
        numbered = "\n".join(f"{offset + i + 1}. {t}" for i, t in enumerate(texts))
        try:
            resp = self.get_bedrock(self._region).converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": _SENTIMENT_SYSTEM}],
                messages=[{"role": "user", "content": [{"text": numbered}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0},
            )
            text_out = resp["output"]["message"]["content"][0]["text"].strip()
            scores = _parse_scores(text_out, len(texts))
            if scores is not None:
                return [_from_score(s) for s in scores]
            self.log.debug("Haiku returned unparseable sentiment output (offset=%d)", offset)
        except Exception as e:
            self.log.warning("Sentiment batch failed (offset=%d): %s", offset, e)
        return [_neutral() for _ in texts]
