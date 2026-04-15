"""Screener mode: find hot stocks under $100, get this week's news, predict movement."""

import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import feedparser
from rich.console import Console

from etf_sentiment.display.screener_dashboard import ScreenerDashboard
from etf_sentiment.market.price_fetcher import PriceFetcher
from etf_sentiment.market.screener import StockScreener
from etf_sentiment.market.stock_predictor import StockPredictor
from etf_sentiment.market.technicals import TechnicalAnalyzer
from etf_sentiment.news.base import Article, ScoredArticle
from etf_sentiment.nlp.sentiment import SentimentAnalyzer

console = Console()


class ScreenerApp:
    """Orchestrates the stock screener pipeline."""

    def __init__(self, max_price: float = 100.0, min_return: float = 10.0, top_n: int = 30):
        self.screener = StockScreener(
            max_price=max_price, min_3m_return=min_return, top_n=top_n
        )
        self.price_fetcher = PriceFetcher(cache_ttl_seconds=600)
        self.tech_analyzer = TechnicalAnalyzer()
        self.predictor = StockPredictor()
        self.sentiment = SentimentAnalyzer()
        self.dashboard = ScreenerDashboard()

    def run(self, cloud_mode: bool = False):
        """Full pipeline: screen → fetch news → analyze → predict → display."""
        console.print("\n[bold cyan]Starting Stock Screener & Predictor...[/bold cyan]\n")
        console.print(f"  Criteria: Price < ${self.screener.max_price}, 3-month return > {self.screener.min_3m_return}%")
        console.print(f"  News window: This week only")
        console.print(f"  Output: {'Cloud (S3 + Email)' if cloud_mode else 'Terminal'}")
        console.print()

        # Step 1: Screen stocks
        screened = self.screener.screen()
        if not screened:
            console.print("[red]No stocks matched the criteria. Try lowering the minimum return.[/red]")
            return

        symbols = [s.symbol for s in screened]
        console.print(f"\n  Top performers: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}\n")

        # Step 2: Fetch this week's news for each screened stock
        console.print("[cyan]Fetching this week's news for screened stocks...[/cyan]")
        stock_articles = self._fetch_weekly_news(symbols)
        total_articles = sum(len(a) for a in stock_articles.values())
        console.print(f"  Fetched {total_articles} articles for {len(stock_articles)} stocks")

        # Step 3: Analyze sentiment
        console.print("[cyan]Analyzing sentiment...[/cyan]")
        scored_articles = self._analyze_sentiment(stock_articles)

        # Step 4: Get technicals (reuse the 3-month data from screener where possible)
        console.print("[cyan]Computing technical indicators...[/cyan]")
        stock_prices = self.price_fetcher.fetch_batch(symbols, period="3mo")
        technicals = self.tech_analyzer.analyze_batch(stock_prices)

        # Step 5: Generate predictions
        console.print("[cyan]Generating predictions...[/cyan]")
        predictions = []
        for stock in screened:
            articles = scored_articles.get(stock.symbol, [])
            ti = technicals.get(stock.symbol)
            pred = self.predictor.predict(stock, articles, ti)
            predictions.append(pred)

        # Sort by overall score
        predictions.sort(key=lambda p: p.overall_score, reverse=True)

        # Step 6: Save to history + check alerts
        alerts = []
        try:
            from etf_sentiment.history import History
            from etf_sentiment.alerts import AlertManager

            history = History()
            history.save_run(
                predictions, self.screener.max_price,
                self.screener.min_3m_return, self.screener.top_n,
            )

            alert_mgr = AlertManager(history)
            alerts = alert_mgr.check_and_alert(predictions)
            history.close()
        except Exception:
            pass

        console.print("[green]Done![/green]\n")

        # Step 7: Output
        if cloud_mode:
            from etf_sentiment.cloud_output import run_cloud_mode
            run_cloud_mode(predictions, len(screened), alerts)
        else:
            self.dashboard.render(predictions, len(screened))
            if alerts:
                from etf_sentiment.alerts import AlertManager
                AlertManager().display_alerts(alerts)

    def _fetch_weekly_news(self, symbols: list[str]) -> dict[str, list[Article]]:
        """Fetch this week's news for each stock symbol via Google News RSS."""
        result = {}
        today = datetime.now(timezone.utc)
        week_ago = today - timedelta(days=7)
        after_str = week_ago.strftime("%Y-%m-%d")
        before_str = today.strftime("%Y-%m-%d")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)
        )

        for symbol in symbols:
            try:
                # Search for stock by ticker and company context
                query = f"{symbol} stock after:{after_str} before:{before_str}"
                url = (
                    f"https://news.google.com/rss/search"
                    f"?q={urllib.request.quote(query)}&hl=en-US&gl=US&ceid=US:en"
                )

                response = opener.open(url, timeout=10)
                raw = response.read()
                feed = feedparser.parse(raw)

                articles = []
                for entry in feed.entries[:10]:
                    try:
                        pub_str = entry.get("published", "")
                        if pub_str:
                            from dateutil import parser as dp
                            published = dp.parse(pub_str).astimezone(timezone.utc)
                        else:
                            published = today

                        title = entry.get("title", "").strip()
                        summary = self._clean_html(entry.get("summary", ""))
                        source = entry.get("source", {}).get("title", "Unknown")
                        link = entry.get("link", "")

                        if title:
                            articles.append(Article(
                                title=title,
                                summary=summary,
                                source=source,
                                url=link,
                                published_at=published,
                            ))
                    except Exception:
                        continue

                if articles:
                    result[symbol] = articles

                # Rate limit: ~1 request per second
                time.sleep(0.5)

            except Exception:
                continue

        return result

    def _analyze_sentiment(
        self, stock_articles: dict[str, list[Article]]
    ) -> dict[str, list]:
        """Run FinBERT on all articles, grouped by stock."""
        result = {}

        # Flatten for batch processing
        all_articles = []
        index_map = []  # (symbol, index_in_list)

        for symbol, articles in stock_articles.items():
            for a in articles:
                all_articles.append(a)
                index_map.append(symbol)

        if not all_articles:
            return result

        texts = [a.raw_text for a in all_articles]
        sentiments = self.sentiment.analyze_batch(texts)

        for article, sentiment, symbol in zip(all_articles, sentiments, index_map):
            scored = ScoredArticle(
                article=article,
                sentiment_label=sentiment.label,
                sentiment_score=sentiment.score,
                normalized_score=sentiment.normalized,
            )
            if symbol not in result:
                result[symbol] = []
            result[symbol].append(scored)

        return result

    @staticmethod
    def _clean_html(text: str) -> str:
        import re
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip()
