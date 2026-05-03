"""NewsAgent — pulls pre-scored news from NewsWatcherAgent cache and publishes to pipeline.

Subscribes to: symbol.screened
Publishes to:  symbol.analysed

All Polygon fetching and Haiku scoring is done proactively by NewsWatcherAgent.
This agent is now a thin pass-through: symbol.screened → cache lookup → symbol.analysed.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from .base import BaseAgent
from .event_bus import EventBus

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
    def __init__(self, bus: EventBus):
        super().__init__(bus, "NewsAgent")
        self._queue = bus.subscribe("symbol.screened")

    async def run(self) -> None:
        self.log.info("NewsAgent ready — reading from NewsWatcherAgent cache")
        while True:
            msg = await self._queue.get()
            asyncio.create_task(self._process(msg["data"]))

    async def _process(self, data: dict) -> None:
        sym = data["symbol"]
        stock = data["screened_stock"]
        try:
            from .news_watcher import NewsWatcherAgent
            scored = NewsWatcherAgent.get_cached(sym)
            if not scored:
                self.log.info("News: %s  0 articles (no recent news cached)", sym)
            else:
                avg = sum(a.normalized_score for a in scored) / len(scored)
                self.log.info("News: %s  %d cached articles  avg_sentiment=%+.2f",
                              sym, len(scored), avg)
            await self.bus.publish("symbol.analysed", {
                "symbol": sym,
                "screened_stock": stock,
                "scored_articles": scored,
            })
        except Exception as exc:
            self.log.error("News error %s: %s", sym, exc)
