"""Scheduler: auto-run the screener on a daily/weekly schedule.

Runs the full screener pipeline on a loop, saves results to history,
checks alerts, and optionally runs backtests.
"""

import time
from datetime import datetime, timezone

from rich.console import Console

from stock_sentiment.alerts import AlertManager
from stock_sentiment.backtester import Backtester
from stock_sentiment.history import History
from stock_sentiment.screener_app import ScreenerApp

console = Console()


class Scheduler:
    """Runs the screener on a schedule with alerts and history tracking."""

    def __init__(
        self,
        min_return: float = 10.0,
        top_n: int = 30,
        interval_hours: float = 1.0,  # Default: hourly
        run_backtest: bool = True,
    ):
        self.app = ScreenerApp(min_return=min_return, top_n=top_n)
        self.history = History()
        self.alerts = AlertManager(self.history)
        self.backtester = Backtester(self.history)
        self.broker = PaperBroker()
        self.interval_hours = interval_hours
        self.run_backtest = run_backtest
        self.min_return = min_return
        self.top_n = top_n

    def run(self):
        """Main scheduling loop."""
        console.print("\n[bold cyan]Starting Scheduled Stock Screener[/bold cyan]\n")
        console.print(f"  Schedule: Every {self._format_interval()}")
        console.print(f"  Criteria: 3M return > {self.min_return}%")
        console.print(f"  Backtest on each run: {'Yes' if self.run_backtest else 'No'}")
        console.print(f"  Alerts: Enabled (desktop + terminal)")
        console.print(f"  History: ~/.stock_screener/history.db")
        console.print()

        run_count = 0
        try:
            while True:
                run_count += 1
                now = datetime.now(timezone.utc)
                
                # Check if market is open before running the heavy AI cycle
                market_open = True
                if hasattr(self, 'broker') and self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        market_open = clock.is_open
                        
                        if market_open:
                            time_to_close = (clock.next_close - clock.timestamp).total_seconds()
                            if time_to_close < 1800:
                                console.print(f"\n[yellow]  Market closes in less than 30 mins. Skipping cycle #{run_count} to avoid closing volatility.[/yellow]")
                                market_open = False
                                
                        if not market_open:
                            console.print(f"\n[yellow]  Market is closed or in volatility buffer. Skipping cycle #{run_count}.[/yellow]")
                            console.print(f"  Next market open: {clock.next_open.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                    except Exception as e:
                        console.print(f"[yellow]Warning: Could not check market hours: {e}[/yellow]")
                
                if market_open:
                    console.print(f"\n[bold]{'='*60}[/bold]")
                    console.print(f"[bold cyan]  Run #{run_count} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}[/bold cyan]")
                    console.print(f"[bold]{'='*60}[/bold]\n")

                    self._execute_cycle()

                # Calculate sleep time
                interval_seconds = self.interval_hours * 3600
                if hasattr(self, 'broker') and self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        time_to_close = (clock.next_close - clock.timestamp).total_seconds()
                        
                        if not clock.is_open or time_to_close < 1800:
                            # Sleep until exactly 30 minutes after the next market open
                            # to completely avoid the highly volatile opening bell.
                            time_to_open = (clock.next_open - clock.timestamp).total_seconds()
                            if time_to_open > 0:
                                interval_seconds = time_to_open + 1800
                    except Exception:
                        pass

                console.print(f"\n[dim]{'─'*60}[/dim]")
                if interval_seconds == self.interval_hours * 3600:
                    console.print(f"[dim]  Next run in {self._format_interval()}. Press Ctrl+C to stop.[/dim]")
                else:
                    hours, remainder = divmod(interval_seconds, 3600)
                    minutes = remainder // 60
                    console.print(f"[dim]  Market closed. Sleeping for {int(hours)}h {int(minutes)}m until next open. Press Ctrl+C to stop.[/dim]")
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
            predictions, self.min_return, self.top_n
        )
        console.print(f"\n[green]  Saved run #{run_id} with {len(predictions)} predictions to history.[/green]")

        # Step 3: Check alerts
        console.print("[cyan]Checking for alerts...[/cyan]")
        alerts = self.alerts.check_and_alert(predictions)
        if alerts:
            self.alerts.display_alerts(alerts)
        else:
            console.print("  [dim]No new alerts.[/dim]")

        # Step 4: Execute trades (Paper Trading)
        if hasattr(self, 'broker') and self.broker:
            self.broker.execute_trades(predictions)

        # Step 5: Run backtest on old predictions
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
          return f"{self.interval_hours:.0f} hours"
