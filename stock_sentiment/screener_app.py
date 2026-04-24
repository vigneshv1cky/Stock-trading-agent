"""Screener mode: find hot stocks, get this week's news, predict movement."""

import ssl
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import feedparser
from rich.console import Console

from stock_sentiment.display.screener_dashboard import ScreenerDashboard
from stock_sentiment.market.price_fetcher import PriceFetcher
from stock_sentiment.market.screener import StockScreener
from stock_sentiment.market.stock_predictor import StockPredictor
from stock_sentiment.market.technicals import TechnicalAnalyzer
from stock_sentiment.news.base import Article, ScoredArticle
from stock_sentiment.nlp.sentiment import SentimentAnalyzer

console = Console()

class ScreenerApp:
    """Orchestrates the stock screener pipeline based on Brain Analysis."""

    def __init__(self, top_n: int = 40):
        self.screener = StockScreener(top_n=top_n)
        self.price_fetcher = PriceFetcher(cache_ttl_seconds=600)
        self.tech_analyzer = TechnicalAnalyzer()
        self.predictor = StockPredictor()
        self.sentiment = SentimentAnalyzer()
        self.dashboard = ScreenerDashboard()

    def run(self, cloud_mode: bool = False, trigger: str = "MANUAL"):
        """Full pipeline: screen → fetch news → analyze → predict → display."""
        print(f"[ScreenerApp] Starting Brain Analysis Run (Trigger: {trigger})...")
        console.print("\n[bold cyan]Starting AI Decision Engine...[/bold cyan]\n")
        console.print(f"  Strategy: Aggressive Hybrid (OR Logic)")
        console.print(f"  Barricades: Institutional RVOL + Earnings Pre-Filter")
        console.print(f"  Output: {'Cloud (S3 + Email)' if cloud_mode else 'Terminal'}")
        console.print()

        # Step 1: Screen/Filter stocks
        print("[ScreenerApp] Step 1: Passing universe through Institutional Barricades...")
        screened = self.screener.screen()
        if not screened:
            return ([], 0, [])

        symbols = [s.symbol for s in screened]
        
        # Step 2: Fetch this week's news
        print(f"[ScreenerApp] Step 2: Fetching news for {len(symbols)} active candidates...")
        stock_articles = self._fetch_weekly_news(symbols)
        total_articles = sum(len(a) for a in stock_articles.values())
        console.print(f"  Fetched {total_articles} articles for analysis.")

        # Step 3: Analyze sentiment
        scored_articles = self._analyze_sentiment(stock_articles)

        # Step 4: Get technicals
        stock_prices = self.price_fetcher.fetch_batch(symbols, period="3mo")
        technicals = self.tech_analyzer.analyze_batch(stock_prices)

        # Step 5: Generate predictions (The Brain)
        predictions = []
        for stock in screened:
            articles = scored_articles.get(stock.symbol, [])
            ti = technicals.get(stock.symbol)
            pred = self.predictor.predict(stock, articles, ti)
            predictions.append(pred)

        predictions.sort(key=lambda p: p.overall_score, reverse=True)

        # Step 6: Save to history + check alerts
        alerts = []
        history = None
        try:
            from stock_sentiment.history import History
            from stock_sentiment.alerts import AlertManager
            history = History()
            history.save_run(predictions, 0.0, self.screener.top_n, trigger_type=trigger)
            alert_mgr = AlertManager(history, disable_notifications=True)
            alerts = alert_mgr.check_and_alert(predictions)
        except Exception as e:
            import traceback
            print(f"[ScreenerApp] ERROR in data persistence: {e}")
            traceback.print_exc()
        finally:
            if history: history.close()

        # Step 7: Output
        if cloud_mode:
            from stock_sentiment.cloud_output import run_cloud_mode
            run_cloud_mode(predictions, len(screened), alerts)
        else:
            self.dashboard.render(predictions, len(screened))

        return (predictions, len(screened), alerts)

    def _fetch_weekly_news(self, symbols: list[str]) -> dict[str, list[Article]]:
        result = {}
        today = datetime.now(timezone.utc)
        week_ago = today - timedelta(days=7)
        after_str = week_ago.strftime("%Y-%m-%d")
        before_str = today.strftime("%Y-%m-%d")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

        for symbol in symbols:
            try:
                query = f"{symbol} stock after:{after_str} before:{before_str}"
                url = f"https://news.google.com/rss/search?q={urllib.request.quote(query)}&hl=en-US&gl=US&ceid=US:en"
                response = opener.open(url, timeout=10)
                feed = feedparser.parse(response.read())

                articles = []
                for entry in feed.entries[:10]:
                    try:
                        title = entry.get("title", "").strip()
                        if title:
                            articles.append(Article(
                                title=title,
                                summary=self._clean_html(entry.get("summary", "")),
                                source=entry.get("source", {}).get("title", "Unknown"),
                                url=entry.get("link", ""),
                                published_at=today, # Fallback
                            ))
                    except: continue
                if articles: result[symbol] = articles
                time.sleep(0.1)
            except: continue
        return result

    def _analyze_sentiment(self, stock_articles: dict[str, list[Article]]) -> dict[str, list]:
        result = {}
        all_articles = []
        index_map = []
        for symbol, articles in stock_articles.items():
            for a in articles:
                all_articles.append(a)
                index_map.append(symbol)
        if not all_articles: return result
        texts = [a.raw_text for a in all_articles]
        sentiments = self.sentiment.analyze_batch(texts)
        for article, sentiment, symbol in zip(all_articles, sentiments, index_map):
            scored = ScoredArticle(article=article, sentiment_label=sentiment.label,
                                  sentiment_score=sentiment.score, normalized_score=sentiment.normalized)
            if symbol not in result: result[symbol] = []
            result[symbol].append(scored)
        return result

    @staticmethod
    def _clean_html(text: str) -> str:
        import re
        clean = re.sub(r"<[^>]+>", " ", text); clean = re.sub(r"\s+", " ", clean)
        return clean.strip()
