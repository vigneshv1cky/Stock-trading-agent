"""News provider abstraction: swap between Google RSS and Alpaca without touching the monitor."""

import asyncio
import os
import ssl
import threading
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import feedparser


@dataclass
class NewsArticle:
    id: str
    symbols: list[str]
    headline: str
    source: str
    published_at: datetime


class BaseNewsProvider(ABC):
    """Abstract news provider. Implement `run()` to stream articles to a handler."""

    @abstractmethod
    async def run(self, handler: Callable) -> None:
        """Stream articles indefinitely, calling handler(NewsArticle) for each new one."""

    @abstractmethod
    def update_symbols(self, symbols: set[str]) -> None:
        """Update the set of symbols to monitor (thread-safe)."""

    @abstractmethod
    def name(self) -> str:
        """Provider identifier string."""


# ---------------------------------------------------------------------------
# Google News RSS — free, no API key, ~1-3 min lag after publication
# ---------------------------------------------------------------------------

class GoogleRSSNewsProvider(BaseNewsProvider):
    """Polls Google News RSS once per symbol per cycle (~90s full rotation).

    One request per symbol with 1s between requests — well within Google's
    informal rate limits. Each symbol gets a fresh check every ~90s.
    """

    _RSS_BASE = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    _CYCLE_SLEEP_S = 60   # sleep between full rotations
    _REQUEST_SLEEP_S = 1  # sleep between per-symbol requests

    def __init__(self):
        self._symbols: set[str] = set()
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def name(self) -> str:
        return "rss"

    def update_symbols(self, symbols: set[str]) -> None:
        with self._lock:
            self._symbols = set(symbols)

    async def run(self, handler: Callable) -> None:
        print("[RSS] Google News RSS provider started.")
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx)
        )
        while True:
            with self._lock:
                symbols = list(self._symbols)

            loop = asyncio.get_running_loop()
            for symbol in symbols:
                try:
                    articles = await loop.run_in_executor(
                        None, self._fetch_symbol, symbol, opener
                    )
                    for article in articles:
                        await handler(article)
                except Exception as e:
                    print(f"[RSS] Error fetching {symbol}: {e}")
                await asyncio.sleep(self._REQUEST_SLEEP_S)

            await asyncio.sleep(self._CYCLE_SLEEP_S)

    def _fetch_symbol(self, symbol: str, opener) -> list[NewsArticle]:
        url = self._RSS_BASE.format(
            query=urllib.parse.quote(f"{symbol} stock")
        )
        try:
            response = opener.open(url, timeout=10)
            feed = feedparser.parse(response.read())
        except Exception:
            return []

        results = []
        for entry in feed.entries[:5]:
            article_id = entry.get("id", entry.get("link", ""))
            if not article_id or article_id in self._seen:
                continue
            headline = entry.get("title", "").strip()
            if not headline:
                continue

            self._seen.add(article_id)
            if len(self._seen) > 10000:
                self._seen = set(list(self._seen)[-5000:])

            try:
                pub = entry.get("published_parsed")
                published_at = (
                    datetime(
                        pub.tm_year, pub.tm_mon, pub.tm_mday,
                        pub.tm_hour, pub.tm_min, pub.tm_sec,
                        tzinfo=timezone.utc,
                    )
                    if pub else datetime.now(timezone.utc)
                )
            except Exception:
                published_at = datetime.now(timezone.utc)

            results.append(NewsArticle(
                id=article_id,
                symbols=[symbol],
                headline=headline,
                source=entry.get("source", {}).get("title", "Google News"),
                published_at=published_at,
            ))

        return results


# ---------------------------------------------------------------------------
# Alpaca — WebSocket news stream (requires paid data subscription)
# ---------------------------------------------------------------------------

class AlpacaNewsProvider(BaseNewsProvider):
    """Real-time news via Alpaca's WebSocket NewsDataStream (paid tier required)."""

    def __init__(self, api_key: str, secret_key: str):
        self._api_key = api_key
        self._secret_key = secret_key
        self._symbols: set[str] = set()
        self._lock = threading.Lock()

    def name(self) -> str:
        return "alpaca"

    def update_symbols(self, symbols: set[str]) -> None:
        with self._lock:
            self._symbols = set(symbols)

    async def run(self, handler: Callable) -> None:
        try:
            from alpaca.data.live import NewsDataStream
        except ImportError:
            print("[Alpaca] alpaca-py not installed — provider disabled.")
            return

        self._handler = handler

        def _run():
            stream = NewsDataStream(api_key=self._api_key, secret_key=self._secret_key)
            stream.subscribe_news(self._on_news, ["*"])
            print("[Alpaca] News stream connected.")
            stream.run()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _run)

    async def _on_news(self, news) -> None:
        with self._lock:
            watchlist = self._symbols.copy()

        symbols = getattr(news, "symbols", []) or []
        relevant = [s for s in symbols if s in watchlist]
        if not relevant:
            return

        article = NewsArticle(
            id=str(getattr(news, "id", id(news))),
            symbols=relevant,
            headline=getattr(news, "headline", ""),
            source=getattr(news, "source", "Alpaca"),
            published_at=getattr(news, "created_at", datetime.now(timezone.utc)),
        )
        await self._handler(article)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_provider(settings: dict) -> BaseNewsProvider | None:
    """Instantiate the provider selected in settings. Returns None if disabled/misconfigured."""
    provider = settings.get("news_provider", "rss")

    if provider == "rss":
        return GoogleRSSNewsProvider()

    if provider == "alpaca":
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        if not api_key or not secret:
            print("[NewsProvider] Alpaca selected but credentials missing from environment.")
            return None
        return AlpacaNewsProvider(api_key=api_key, secret_key=secret)

    return None  # "none" / disabled
