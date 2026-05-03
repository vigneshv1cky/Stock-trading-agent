"""NewsWatcherAgent — streams Polygon news WebSocket for real-time catalyst detection.

  • Connects to Polygon wss://socket.polygon.io/stocks and subscribes to N.* (all news)
  • Articles are queued as they arrive; a consumer batches them over a 5-second window
  • Batch is scored with Haiku; results cached per symbol (30-min TTL)
  • Fires market.signal with trigger_type="NEWS_CATALYST" when |avg_sentiment| ≥ 0.45
  • Class-level get_cached(sym) replaces per-symbol Polygon calls in NewsAgent
  • 10-min per-symbol cooldown prevents flooding the pipeline on article bursts
"""

import asyncio
import os
import time
from datetime import datetime, timezone

from .base import BaseAgent
from .event_bus import EventBus
from .news import (
    Article,
    ScoredArticle,
    SentimentResult,
    _HAIKU_MODEL,
    _SENTIMENT_SYSTEM,
    _from_score,
    _neutral,
    _parse_scores,
)

_POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "")
_CACHE_TTL_S = 1800            # 30-min cache per symbol
_SENTIMENT_THRESHOLD = 0.45    # |avg_sentiment| ≥ this fires a NEWS_CATALYST signal
_FIRE_COOLDOWN_S = 600         # 10-min per-symbol cooldown on NEWS_CATALYST firing
_COLLECT_WINDOW_S = 5.0        # collect articles for 5s before scoring the batch
_BATCH_SIZE = 50
_WS_RECONNECT_MAX_S = 120      # cap reconnect backoff at 2 min


class NewsWatcherAgent(BaseAgent):
    _cache: dict[str, tuple[float, list[ScoredArticle]]] = {}  # {sym: (ts, articles)}
    _fired_cooldown: dict[str, float] = {}                     # {sym: last_fired_ts}

    def __init__(self, bus: EventBus, universe: list[str]):
        super().__init__(bus, "NewsWatcherAgent")
        self._universe = set(universe)
        self._seen_ids: set[str] = set()
        self._region = os.environ.get("AWS_REGION", "us-east-1")

    @classmethod
    def get_cached(cls, sym: str) -> list[ScoredArticle]:
        """Return cached ScoredArticles for sym; empty list if stale or missing."""
        entry = cls._cache.get(sym)
        if not entry:
            return []
        ts, articles = entry
        if time.time() - ts > _CACHE_TTL_S:
            return []
        return articles

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        if not _POLYGON_KEY:
            self.log.warning("No POLYGON_API_KEY — NewsWatcherAgent idle (no news catalyst signals)")
            while True:
                await asyncio.sleep(3600)
            return  # type: ignore[unreachable]

        queue: asyncio.Queue[tuple[str, Article]] = asyncio.Queue()
        self.log.info("NewsWatcherAgent started — Polygon news WebSocket, universe=%d symbols",
                      len(self._universe))

        scorer = asyncio.create_task(self._score_consumer(queue))
        try:
            await self._stream_news(queue)
        finally:
            scorer.cancel()

    # ------------------------------------------------------------------
    # WebSocket stream — reconnects on any error
    # ------------------------------------------------------------------

    async def _stream_news(self, queue: asyncio.Queue) -> None:
        backoff = 5.0
        while True:
            try:
                from polygon.websocket import WebSocketClient
                from polygon.websocket.models import Feed, Market

                client = WebSocketClient(
                    api_key=_POLYGON_KEY,
                    feed=Feed.RealTime,
                    market=Market.Stocks,
                    subscriptions=["N.*"],
                    verbose=False,
                )

                async def on_msg(msgs: list) -> None:
                    for msg in msgs:
                        try:
                            await self._ingest(msg, queue)
                        except Exception:
                            pass

                self.log.info("Polygon news WebSocket connected")
                backoff = 5.0
                await client.connect(on_msg)
                # connect() returned cleanly — server closed connection
                self.log.info("Polygon news WebSocket closed — reconnecting")

            except ImportError:
                self.log.error("polygon-api-client WebSocket not available — "
                               "install polygon-api-client >= 1.0")
                await asyncio.sleep(3600)
                return
            except Exception as exc:
                self.log.warning("News WebSocket error: %s — retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _WS_RECONNECT_MAX_S)

    # ------------------------------------------------------------------
    # Ingest one WebSocket message
    # ------------------------------------------------------------------

    async def _ingest(self, msg, queue: asyncio.Queue) -> None:
        tickers = getattr(msg, "tickers", None)
        if not tickers:
            return  # status / auth / non-news event

        relevant = [t for t in tickers if t in self._universe]
        if not relevant:
            return

        article_id: str = str(getattr(msg, "id", "") or getattr(msg, "article_url", ""))
        if article_id in self._seen_ids:
            return
        self._seen_ids.add(article_id)

        # Prune seen_ids to prevent unbounded growth
        if len(self._seen_ids) > 5000:
            self._seen_ids = set(list(self._seen_ids)[-3000:])

        title = getattr(msg, "title", "") or ""
        if not title:
            return

        now = datetime.now(timezone.utc)
        published_at = now
        pub_utc = getattr(msg, "published_utc", None)
        if pub_utc:
            try:
                published_at = datetime.fromisoformat(str(pub_utc).replace("Z", "+00:00"))
            except Exception:
                pass

        publisher = getattr(msg, "publisher", None)
        source = publisher.name if (publisher and hasattr(publisher, "name")) else "Polygon"
        url = getattr(msg, "article_url", "") or ""
        description = getattr(msg, "description", "") or ""

        article = Article(title=title, summary=description, source=source,
                          url=url, published_at=published_at)

        for sym in relevant:
            await queue.put((sym, article))

    # ------------------------------------------------------------------
    # Score consumer — batches articles, scores with Haiku, fires signals
    # ------------------------------------------------------------------

    async def _score_consumer(self, queue: asyncio.Queue) -> None:
        loop = asyncio.get_running_loop()
        while True:
            # Block until at least one article arrives
            sym, first_art = await queue.get()
            batch: list[tuple[str, Article]] = [(sym, first_art)]

            # Collect any additional articles within the 5-second window
            deadline = loop.time() + _COLLECT_WINDOW_S
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            # Score in executor (Haiku I/O is blocking)
            scored_by_sym = await loop.run_in_executor(None, self._score_and_cache, batch)

            # Fire signals back in the asyncio context
            for s, new_scored in scored_by_sym.items():
                await self._maybe_fire(s, new_scored)

    def _score_and_cache(
        self, batch: list[tuple[str, Article]]
    ) -> dict[str, list[ScoredArticle]]:
        by_sym: dict[str, list[Article]] = {}
        for sym, art in batch:
            by_sym.setdefault(sym, []).append(art)

        all_arts: list[Article] = []
        sym_ranges: list[tuple[str, int, int]] = []
        for sym, arts in by_sym.items():
            start = len(all_arts)
            all_arts.extend(arts)
            sym_ranges.append((sym, start, len(all_arts)))

        scored_all = self._score_articles(all_arts)

        result: dict[str, list[ScoredArticle]] = {}
        now_ts = time.time()
        for sym, start, end in sym_ranges:
            new_scored = scored_all[start:end]
            _, existing = NewsWatcherAgent._cache.get(sym, (0.0, []))
            merged = new_scored + existing
            NewsWatcherAgent._cache[sym] = (now_ts, merged[:10])
            result[sym] = new_scored

        return result

    async def _maybe_fire(self, sym: str, new_scored: list[ScoredArticle]) -> None:
        if not new_scored:
            return
        now_ts = time.time()
        if now_ts - NewsWatcherAgent._fired_cooldown.get(sym, 0.0) < _FIRE_COOLDOWN_S:
            return

        avg_sentiment = sum(a.normalized_score for a in new_scored) / len(new_scored)
        if abs(avg_sentiment) < _SENTIMENT_THRESHOLD:
            return

        NewsWatcherAgent._fired_cooldown[sym] = now_ts
        direction = "UP" if avg_sentiment > 0 else "DOWN"
        self.log.info(
            "NEWS_CATALYST: %s  avg_sentiment=%+.2f  articles=%d  dir=%s",
            sym, avg_sentiment, len(new_scored), direction,
        )
        await self.bus.publish("market.signal", {
            "symbol": sym,
            "price": 0.0,             # screener fetches real price
            "rvol": 0.0,              # screener computes real RVOL
            "price_change_pct": 0.0,
            "vol_direction": direction,
            "trigger_type": "NEWS_CATALYST",
            "avg_sentiment": round(avg_sentiment, 3),
            "article_count": len(new_scored),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------
    # Sentiment scoring — Haiku batch call
    # ------------------------------------------------------------------

    def _score_articles(self, articles: list[Article]) -> list[ScoredArticle]:
        if not articles:
            return []
        texts = [a.raw_text for a in articles]
        results: list[SentimentResult] = []
        for batch_start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[batch_start: batch_start + _BATCH_SIZE]
            results.extend(self._score_batch(batch, batch_start))
        return [
            ScoredArticle(
                article=art,
                sentiment_label=res.label,
                sentiment_score=res.score,
                normalized_score=res.normalized,
            )
            for art, res in zip(articles, results)
        ]

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
        except Exception as e:
            self.log.warning("NewsWatcher sentiment batch failed (offset=%d): %s", offset, e)
        return [_neutral() for _ in texts]
