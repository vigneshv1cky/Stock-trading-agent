"""Real-time news monitor: acts on breaking news for held positions and watchlist candidates.

Runs alongside the 30-min scheduler in a background daemon thread.
The news provider (Polygon / Alpaca / disabled) is loaded from ~/.stock_screener/settings.json
and re-initialised automatically when the provider setting changes.

Two-stage pipeline per article:
  1. Nova Micro quick score  — cheap filter (|score| must exceed threshold)
  2. Claude Haiku deep call  — red_flag detection + conviction score
Actions:
  - Red flag on held position   → immediate close
  - Strong catalyst on candidate → entry attempt (symbol must be on watchlist)
"""

import asyncio
import json
import os
import threading
from datetime import datetime, timezone

import boto3
from rich.console import Console

from stock_sentiment.config import load_settings
from stock_sentiment.market.news_providers import BaseNewsProvider, NewsArticle, build_provider
from stock_sentiment.nlp.sentiment import SentimentAnalyzer

console = Console()

_HAIKU_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_NOVA_THRESHOLD = 0.65    # abs(score) below this → ignore (noise)
_ENTRY_MIN_SCORE = 78     # Haiku conviction required for a real-time entry
_ACTION_COOLDOWN_MIN = 15  # minutes between actions on the same symbol


class NewsMonitor:
    """Watches live news and reacts to high-confidence events between screener cycles."""

    def __init__(self, broker):
        self.broker = broker
        self._watchlist: set[str] = set()
        self._held: set[str] = set()
        self._cooldowns: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._nova = SentimentAnalyzer()
        self._bedrock = None
        self._region = os.environ.get("AWS_REGION", "us-east-1")
        self._provider: BaseNewsProvider | None = None
        self._active_provider_name: str = ""

    # ------------------------------------------------------------------
    # Watchlist management (called from scheduler thread after each cycle)
    # ------------------------------------------------------------------

    def update_watchlist(self, predictions: list, held_symbols: set):
        """Refresh monitored symbols and reinitialise provider if settings changed."""
        candidates = {p.symbol for p in predictions[:20] if p.prediction != "BEARISH"}
        with self._lock:
            self._held = set(held_symbols)
            self._watchlist = candidates | self._held

        console.print(
            f"  [dim]News watchlist:[/dim] [bold]{len(self._watchlist)} symbols[/bold]"
            f"  [dim]({len(self._held)} held · {len(candidates - self._held)} candidates)[/dim]"
        )

        settings = load_settings()
        new_name = settings.get("news_provider", "rss")
        if new_name != self._active_provider_name:
            console.print(f"  [dim]News provider changed: {self._active_provider_name!r} → [bold]{new_name}[/bold][/dim]")
            self._restart_provider(settings)

        if self._provider:
            self._provider.update_symbols(self._watchlist)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the news stream in a background daemon thread."""
        settings = load_settings()
        self._restart_provider(settings)

    def _restart_provider(self, settings: dict):
        """(Re)build the provider and launch it in a fresh daemon thread."""
        self._provider = build_provider(settings)
        self._active_provider_name = settings.get("news_provider", "rss")

        if self._provider is None:
            console.print("[dim]  News monitor inactive — no provider configured.[/dim]")
            return

        if self._watchlist:
            self._provider.update_symbols(self._watchlist)

        thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"news-{self._active_provider_name}"
        )
        thread.start()
        console.print(f"  [dim]News monitor started — provider: [bold]{self._active_provider_name}[/bold][/dim]")

    def _run_loop(self):
        provider = self._provider
        if provider is None:
            return
        try:
            asyncio.run(provider.run(self._on_article))
        except Exception as e:
            console.print(f"  [yellow]⚠  News provider loop exited: {e}[/yellow]")

    # ------------------------------------------------------------------
    # Article handler
    # ------------------------------------------------------------------

    async def _on_article(self, article: NewsArticle):
        with self._lock:
            watchlist = self._watchlist.copy()
            held = self._held.copy()

        relevant = [s for s in article.symbols if s in watchlist]
        if not relevant or not article.headline:
            return

        for symbol in relevant:
            if self._in_cooldown(symbol):
                continue

            # Stage 1 — Nova Micro quick score
            result = self._nova.analyze(article.headline)
            if abs(result.normalized) < _NOVA_THRESHOLD:
                continue

            console.print(
                f"  [dim]News[/dim] [bold]{symbol}[/bold]"
                f"  nova=[bold]{result.normalized:+.2f}[/bold]"
                f"  [dim]{article.headline[:80]} ({article.source})[/dim]"
            )

            # Stage 2 — Haiku deep analysis
            llm = self._haiku_analyze(symbol, article.headline, result.normalized)

            if llm.get("red_flag") and symbol in held:
                console.print(
                    f"  [bold red]🚨 RED FLAG EXIT {symbol}[/bold red]"
                    f"  haiku={llm.get('score', '?')}"
                    f"  [dim]{llm.get('reasoning', '')}[/dim]"
                )
                try:
                    self.broker._close_position_safely(symbol)
                    self._set_cooldown(symbol)
                except Exception as e:
                    console.print(f"  [red]✖  Close failed for {symbol}: {e}[/red]")

            elif (
                not llm.get("red_flag")
                and llm.get("score", 0) >= _ENTRY_MIN_SCORE
                and result.normalized > 0
                and symbol not in held
            ):
                console.print(
                    f"  [bold green]⚡ CATALYST ENTRY {symbol}[/bold green]"
                    f"  haiku={llm['score']}"
                    f"  [dim]{llm.get('reasoning', '')}[/dim]"
                )
                self._attempt_entry(symbol, llm["score"])
                self._set_cooldown(symbol)

    # ------------------------------------------------------------------
    # Haiku single-stock analysis
    # ------------------------------------------------------------------

    def _haiku_analyze(self, symbol: str, headline: str, nova_score: float) -> dict:
        prompt = (
            f'Breaking news on {symbol}: "{headline[:300]}"\n'
            f"Initial sentiment: {nova_score:+.2f}\n"
            "Identify: catalyst (M&A, earnings beat, FDA approval, analyst upgrade) "
            "or red flag (SEC/DOJ, lawsuit, guidance cut, CEO departure, recall).\n"
            'Return ONLY JSON: {"score": 0-100, "red_flag": bool, "reasoning": "one sentence"}'
        )
        try:
            resp = self._get_bedrock().converse(
                modelId=_HAIKU_MODEL,
                system=[{"text": "You are a financial news analyst for equity swing trading. Be concise."}],
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0},
            )
            text = resp["output"]["message"]["content"][0]["text"]
            return json.loads(text)
        except Exception as e:
            console.print(f"  [yellow]⚠  Haiku call failed for {symbol}: {e}[/yellow]")
            return {"score": 50, "red_flag": False, "reasoning": "unavailable"}

    # ------------------------------------------------------------------
    # Entry helper
    # ------------------------------------------------------------------

    def _attempt_entry(self, symbol: str, score: float):
        if not self.broker.client:
            return
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest

            api_key = os.environ.get("ALPACA_API_KEY", "")
            secret = os.environ.get("ALPACA_SECRET_KEY", "")
            data_client = StockHistoricalDataClient(api_key, secret)
            trade = data_client.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )
            price = float(trade[symbol].price)
            account = self.broker.client.get_account()
            portfolio_value = float(account.equity)
            self.broker._place_market_buy(symbol, score, price, portfolio_value)
        except Exception as e:
            console.print(f"  [red]✖  Entry attempt failed for {symbol}: {e}[/red]")

    # ------------------------------------------------------------------
    # Cooldown helpers
    # ------------------------------------------------------------------

    def _in_cooldown(self, symbol: str) -> bool:
        ts = self._cooldowns.get(symbol)
        if not ts:
            return False
        return (datetime.now(timezone.utc) - ts).total_seconds() < _ACTION_COOLDOWN_MIN * 60

    def _set_cooldown(self, symbol: str):
        self._cooldowns[symbol] = datetime.now(timezone.utc)

    def _get_bedrock(self):
        if self._bedrock is None:
            self._bedrock = boto3.client("bedrock-runtime", region_name=self._region)
        return self._bedrock
