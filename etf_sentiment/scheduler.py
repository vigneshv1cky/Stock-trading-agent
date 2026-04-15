"""Scheduler: auto-run the screener on a daily/weekly schedule.

Runs the full screener pipeline on a loop, saves results to history,
checks alerts, and optionally runs backtests.
"""

import time
from datetime import datetime, timezone

from rich.console import Console

from etf_sentiment.alerts import AlertManager
from etf_sentiment.backtester import Backtester
from etf_sentiment.history import History
from etf_sentiment.screener_app import ScreenerApp

console = Console()


class Scheduler:
    """Runs the screener on a schedule with alerts and history tracking."""

    def __init__(
        self,
        max_price: float = 100.0,
        min_return: float = 10.0,
        top_n: int = 30,
        interval_hours: float = 24.0,  # Default: daily
        run_backtest: bool = True,
    ):
        self.app = ScreenerApp(max_price=max_price, min_return=min_return, top_n=top_n)
        self.history = History()
        self.alerts = AlertManager(self.history)
        self.backtester = Backtester(self.history)
        self.interval_hours = interval_hours
        self.run_backtest = run_backtest
        self.max_price = max_price
        self.min_return = min_return
        self.top_n = top_n

    def run(self):
        """Main scheduling loop."""
        console.print("\n[bold cyan]Starting Scheduled Stock Screener[/bold cyan]\n")
        console.print(f"  Schedule: Every {self._format_interval()}")
        console.print(f"  Criteria: Price < ${self.max_price}, 3M return > {self.min_return}%")
        console.print(f"  Backtest on each run: {'Yes' if self.run_backtest else 'No'}")
        console.print(f"  Alerts: Enabled (desktop + terminal)")
        console.print(f"  History: ~/.stock_screener/history.db")
        console.print()

        run_count = 0
        try:
            while True:
                run_count += 1
                now = datetime.now(timezone.utc)
                console.print(f"\n[bold]{'='*60}[/bold]")
                console.print(f"[bold cyan]  Run #{run_count} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}[/bold cyan]")
                console.print(f"[bold]{'='*60}[/bold]\n")

                self._execute_cycle()

                # Calculate next run
                next_run = datetime.now(timezone.utc)
                interval_seconds = self.interval_hours * 3600
                next_time = next_run.strftime('%H:%M')

                console.print(f"\n[dim]{'─'*60}[/dim]")
                console.print(
                    f"[dim]  Next run in {self._format_interval()}. "
                    f"Press Ctrl+C to stop.[/dim]"
                )
                console.print(f"[dim]{'─'*60}[/dim]\n")

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            console.print("\n[yellow]Scheduler stopped.[/yellow]")
            console.print(f"  Completed {run_count} run{'s' if run_count != 1 else ''}.")
            console.print(f"  History saved to: ~/.stock_screener/history.db")
            console.print(f"  Run 'python run.py --backtest' to check prediction accuracy.\n")
        finally:
            self.history.close()

    def _execute_cycle(self):
        """Run one full cycle: screen → predict → save → alert → backtest."""

        # Step 1: Run the screener
        predictions = self._run_screener()
        if not predictions:
            return

        # Step 2: Save to history
        run_id = self.history.save_run(
            predictions, self.max_price, self.min_return, self.top_n
        )
        console.print(f"\n[green]  Saved run #{run_id} with {len(predictions)} predictions to history.[/green]")

        # Step 3: Check alerts
        console.print("[cyan]Checking for alerts...[/cyan]")
        alerts = self.alerts.check_and_alert(predictions)
        if alerts:
            self.alerts.display_alerts(alerts)
        else:
            console.print("  [dim]No new alerts.[/dim]")

        # Step 4: Run backtest on old predictions
        if self.run_backtest:
            console.print()
            self.backtester.run(min_age_days=5)

    def _run_screener(self) -> list:
        """Run the screener and return predictions (without displaying dashboard)."""
        screened = self.app.screener.screen()
        if not screened:
            console.print("[red]No stocks matched criteria.[/red]")
            return []

        symbols = [s.symbol for s in screened]

        # Fetch news
        console.print("[cyan]Fetching this week's news...[/cyan]")
        stock_articles = self.app._fetch_weekly_news(symbols)
        total = sum(len(a) for a in stock_articles.values())
        console.print(f"  Fetched {total} articles for {len(stock_articles)} stocks")

        # Sentiment
        console.print("[cyan]Analyzing sentiment...[/cyan]")
        scored_articles = self.app._analyze_sentiment(stock_articles)

        # Technicals
        console.print("[cyan]Computing technicals...[/cyan]")
        stock_prices = self.app.price_fetcher.fetch_batch(symbols, period="3mo")
        technicals = self.app.tech_analyzer.analyze_batch(stock_prices)

        # Predictions
        console.print("[cyan]Generating predictions...[/cyan]")
        predictions = []
        for stock in screened:
            articles = scored_articles.get(stock.symbol, [])
            ti = technicals.get(stock.symbol)
            pred = self.app.predictor.predict(stock, articles, ti)
            predictions.append(pred)

        predictions.sort(key=lambda p: p.overall_score, reverse=True)

        # Display
        self.app.dashboard.render(predictions, len(screened))

        return predictions

    def _format_interval(self) -> str:
        if self.interval_hours >= 24:
            days = self.interval_hours / 24
            if days == 1:
                return "24 hours (daily)"
            elif days == 7:
                return "7 days (weekly)"
            else:
                return f"{days:.0f} days"
        else:
            return f"{self.interval_hours:.0f} hours"
