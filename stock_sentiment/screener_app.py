"""Screener mode: find hot stocks, get this week's news, predict movement."""

import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import feedparser

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]
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

    def __init__(self, top_n: int = 40, min_return: float = 10.0):  # noqa: ARG002
        self.screener = StockScreener(top_n=top_n)
        self.price_fetcher = PriceFetcher(cache_ttl_seconds=600)
        self.tech_analyzer = TechnicalAnalyzer()
        self.predictor = StockPredictor()
        self.sentiment = SentimentAnalyzer()
        self.dashboard = ScreenerDashboard()

    def run(self, trigger: str = "MANUAL", execute_trades: bool = True):
        """Full pipeline: screen → fetch news → analyze → predict → execute → display."""
        from datetime import datetime as _dt
        now_str = _dt.now(_ET).strftime("%Y-%m-%d %H:%M ET")

        # ── Header ────────────────────────────────────────────────────────
        console.rule("[bold cyan]🧠  AI Decision Engine[/bold cyan]")
        console.print(
            f"  [dim]Trigger:[/dim] [yellow]{trigger}[/yellow]  ·  {now_str}"
        )
        console.rule()
        console.print()

        # Step 1: Screen/Filter stocks
        console.print("  [dim][1/5][/dim] Screening universe through Institutional Barricades...")
        screened = self.screener.screen()
        if not screened:
            console.print("  [red]✗  No stocks passed filters.[/red]")
            return ([], 0, [])

        breakouts  = sum(1 for s in screened if s.archetype == "BREAKOUT")
        momentum   = sum(1 for s in screened if s.archetype == "MOMENTUM")
        recoveries = sum(1 for s in screened if s.archetype == "RECOVERY")
        top_rvol   = sorted(screened, key=lambda s: s.volume_ratio, reverse=True)[:5]
        top_str    = "  ·  ".join(f"[bold]{s.symbol}[/bold] {s.volume_ratio:.1f}x" for s in top_rvol)

        console.print(
            f"  [green]✓[/green]  [bold]{len(screened)} stocks[/bold] passed"
            f"  [dim]·[/dim]  [cyan]{breakouts} Breakout[/cyan]"
            f"  [dim]·[/dim]  [green]{momentum} Momentum[/green]"
            f"  [dim]·[/dim]  [yellow]{recoveries} Recovery[/yellow]"
        )
        console.print(f"  [dim]Top RVOL:[/dim]  {top_str}\n")

        symbols = [s.symbol for s in screened]

        # Step 2: Fetch this week's news
        console.print(f"  [dim][2/5][/dim] Fetching news for {len(symbols)} candidates...")
        stock_articles = self._fetch_weekly_news(symbols)
        total_articles = sum(len(a) for a in stock_articles.values())
        no_news        = [s for s in symbols if s not in stock_articles]
        avg_articles   = total_articles / len(stock_articles) if stock_articles else 0
        top_covered    = sorted(stock_articles.items(), key=lambda x: len(x[1]), reverse=True)[:3]
        top_cov_str    = "  ·  ".join(f"[bold]{s}[/bold] {len(a)}" for s, a in top_covered)

        console.print(
            f"  [green]✓[/green]  [bold]{total_articles} articles[/bold]"
            f"  [dim]·[/dim]  avg {avg_articles:.1f}/stock"
            f"  [dim]·[/dim]  most covered: {top_cov_str}"
            + (f"\n  [dim]No news:[/dim] [yellow]{', '.join(no_news)}[/yellow]" if no_news else "")
        )
        console.print()

        # News feed health check — <20% coverage signals rate-limit or SSL failure
        news_health_alert: dict | None = None
        news_coverage = len(stock_articles) / len(symbols) if symbols else 0.0
        if news_coverage < 0.20:
            _status = "DEAD" if total_articles == 0 else "DEGRADED"
            _msg = (
                f"RSS feed {_status}: only {len(stock_articles)}/{len(symbols)} stocks returned articles "
                f"({news_coverage:.0%} coverage, {total_articles} total). "
                "Google may be rate-limiting this IP or an SSL/network issue occurred — "
                "sentiment scores will trend neutral and LLM sees 'No recent news'. "
                "Treat this cycle's predictions with caution."
            )
            console.print(f"  [bold red]⚠  NEWS FEED {_status}[/bold red]  [red]{_msg}[/red]\n")
            news_health_alert = {
                "alert_type": f"NEWS_FEED_{_status}",
                "symbol": "SYSTEM",
                "message": _msg,
                "prediction": "NEUTRAL",
                "score": 0.0,
                "price": 0.0,
            }

        # Step 3: Analyze sentiment
        console.print(f"  [dim][3/5][/dim] Scoring {total_articles} articles via Nova Micro...")
        scored_articles = self._analyze_sentiment(stock_articles)

        all_scored = [a for arts in scored_articles.values() for a in arts]
        pos   = sum(1 for a in all_scored if a.sentiment_label == "positive")
        neg   = sum(1 for a in all_scored if a.sentiment_label == "negative")
        neut  = len(all_scored) - pos - neg
        avg_s = sum(a.normalized_score for a in all_scored) / len(all_scored) if all_scored else 0

        console.print(
            f"  [green]✓[/green]  [bold]{len(all_scored)} articles[/bold] scored"
            f"  [dim]·[/dim]  [green]{pos} positive[/green]"
            f"  [dim]·[/dim]  [red]{neg} negative[/red]"
            f"  [dim]·[/dim]  [dim]{neut} neutral[/dim]"
            f"  [dim]·[/dim]  avg sentiment [bold]{avg_s:+.2f}[/bold]"
        )
        console.print()

        # Step 4: Get technicals
        console.print(f"  [dim][4/5][/dim] Fetching technicals for {len(symbols)} symbols via Alpaca...")
        stock_prices = self.price_fetcher.fetch_batch(symbols, period="3mo")
        technicals = self.tech_analyzer.analyze_batch(stock_prices)
        near = sorted(
            [(s.symbol, s.days_to_earnings) for s in screened
             if s.days_to_earnings is not None and s.days_to_earnings <= 14],
            key=lambda x: x[1],
        )
        near_str = ("  ·  Earnings: [yellow]" + "  ·  ".join(f"{s} {d}d" for s, d in near[:6]) + "[/yellow]") if near else ""
        console.print(f"  [green]✓[/green]  [bold]{len(technicals)} symbols[/bold]{near_str}\n")

        # Step 5: Generate predictions (The Brain) — one Bedrock call for all stocks
        console.print(f"  [dim][5/5][/dim] Brain analysis — Claude Haiku scoring {len(screened)} stocks...")
        predictions = self.predictor.predict_all(screened, scored_articles, technicals)
        predictions.sort(key=lambda p: p.overall_score, reverse=True)
        bullish = sum(1 for p in predictions if p.prediction == "BULLISH")
        bearish_preds = [p for p in predictions if p.prediction == "BEARISH"]
        neutral = len(predictions) - bullish - len(bearish_preds)
        console.print(
            f"  [green]✓[/green]  [green]{bullish} BULLISH[/green]  ·  "
            f"[dim]{neutral} NEUTRAL[/dim]  ·  [red]{len(bearish_preds)} BEARISH[/red]"
        )
        if bearish_preds:
            flags = "  ·  ".join(p.symbol for p in bearish_preds)
            console.print(f"     [red]🚩 Red flags:[/red] {flags}")
        console.print()
        console.rule()

        # Step 6: Save to history + check alerts
        alerts = []
        history = None
        run_id = None
        try:
            from stock_sentiment.history import History
            from stock_sentiment.alerts import AlertManager
            history = History()
            run_id = history.save_run(predictions, 0.0, self.screener.top_n, trigger_type=trigger)
            alert_mgr = AlertManager(history, disable_notifications=True)
            alerts = alert_mgr.check_and_alert(predictions)
            if news_health_alert:
                alerts.insert(0, news_health_alert)
                try:
                    history.save_alert(**news_health_alert)
                except Exception:
                    pass
        except Exception as e:
            import traceback
            print(f"[ScreenerApp] ERROR in data persistence: {e}")
            traceback.print_exc()

        # Step 7: Execute trades via Alpaca (regime sets threshold + sizing)
        if execute_trades:
            try:
                from stock_sentiment.market.broker import PaperBroker
                broker = PaperBroker()
                exec_log = broker.execute_trades(predictions, trigger=trigger)
                if history and run_id and exec_log:
                    for trade in exec_log.get("bought", []) + [
                        {"symbol": t["in"], "trade_type": t.get("trade_type", "SWING")}
                        for t in exec_log.get("swapped", [])
                    ]:
                        history.update_trade_type(trade["symbol"], run_id, trade.get("trade_type", "SWING"))
            except Exception as e:
                print(f"[ScreenerApp] Broker error: {e}")
        else:
            print("[ScreenerApp] Step 7: Trade execution skipped (screen-only mode).")

        if history:
            history.close()

        # Step 8: Output
        self.dashboard.render(predictions, len(screened))

        return (predictions, len(screened), alerts)

    def _fetch_weekly_news(self, symbols: list[str]) -> dict[str, list[Article]]:
        result: dict[str, list[Article]] = {}
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
                url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
                response = opener.open(url, timeout=10)
                feed = feedparser.parse(response.read())

                articles = []
                for entry in feed.entries[:10]:
                    try:
                        title = entry.get("title", "").strip()
                        if title:
                            pub = entry.get("published_parsed")
                            published_at = (
                                datetime(
                                    pub.tm_year, pub.tm_mon, pub.tm_mday,
                                    pub.tm_hour, pub.tm_min, pub.tm_sec,
                                    tzinfo=timezone.utc,
                                )
                                if pub else today
                            )
                            articles.append(Article(
                                title=title,
                                summary=self._clean_html(entry.get("summary", "")),
                                source=entry.get("source", {}).get("title", "Unknown"),
                                url=entry.get("link", ""),
                                published_at=published_at,
                            ))
                    except Exception:
                        continue
                if articles:
                    result[symbol] = articles
                time.sleep(0.1)
            except Exception:
                continue
        return result

    def _analyze_sentiment(self, stock_articles: dict[str, list[Article]]) -> dict[str, list]:
        result: dict[str, list] = {}
        all_articles = []
        index_map = []
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
