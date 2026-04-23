"""Scheduler: auto-run the screener on a daily/weekly schedule.

Runs the full screener pipeline on a loop, saves results to history,
checks alerts, and optionally runs backtests.
"""

import time
from datetime import datetime, timezone, timedelta

from rich.console import Console

from stock_sentiment.alerts import AlertManager
from stock_sentiment.backtester import Backtester
from stock_sentiment.history import History
from stock_sentiment.screener_app import ScreenerApp
from stock_sentiment.market.broker import PaperBroker

console = Console()


class Scheduler:
    """Runs the screener on a schedule with alerts and history tracking."""

    def __init__(
        self,
        min_return: float = 10.0,
        top_n: int = 30,
        interval_hours: float = 0.5,  # Default: 30 mins
        run_backtest: bool = True,
    ):
        self.app = ScreenerApp(min_return=min_return, top_n=top_n)
        self.history = History()
        self.alerts = AlertManager(self.history, disable_notifications=True)
        self.backtester = Backtester(self.history)
        self.broker = PaperBroker()
        self.interval_hours = interval_hours
        self.run_backtest = run_backtest
        self.min_return = min_return
        self.top_n = top_n
        self.run_count = 0

    def run(self):
        """Main scheduling loop."""
        console.print("\n[bold cyan]Starting Scheduled Stock Screener[/bold cyan]\n")
        console.print(f"  Schedule: Every {self._format_interval()}")
        console.print(f"  Criteria: 3M return > {self.min_return}%")
        console.print(f"  Backtest on each run: {'Yes' if self.run_backtest else 'No'}")
        console.print(f"  Alerts: Enabled (desktop + terminal)")
        storage_type = "Amazon DynamoDB" if os.environ.get("ENV") == "PROD" else f"SQLite (~/.stock_screener/local_history.db)"
        console.print(f"  History:  {storage_type}")
        console.print()

        try:
            while True:
                time.sleep(1)  # Small yield to GIL
                self.run_count += 1
                now = datetime.now(timezone.utc)
                
                # Check if market is open before running the heavy AI cycle
                market_open = True
                status_msg = "Market Open - Initializing Cycle"
                if hasattr(self, 'broker') and self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        market_open = clock.is_open
                        
                        if market_open:
                            time_to_close = (clock.next_close - clock.timestamp).total_seconds()
                            if time_to_close < 1800:
                                console.print(f"\n[yellow]  Market closes in less than 30 mins. Skipping cycle #{self.run_count} to avoid closing volatility.[/yellow]")
                                market_open = False
                                status_msg = "Market Closing Soon - Skipping Cycle"
                                
                        if not market_open:
                            next_open = clock.next_open.strftime('%Y-%m-%d %H:%M:%S UTC')
                            console.print(f"\n[yellow]  Market is closed or in volatility buffer. Skipping cycle #{self.run_count}.[/yellow]")
                            console.print(f"  Next market open: {next_open}")
                            status_msg = f"Market Closed until {next_open}"
                    except Exception as e:
                        console.print(f"[yellow]Warning: Could not check market hours: {e}[/yellow]")
                        status_msg = "Market Status Unknown - Retrying"
                
                self.history.save_heartbeat("Checking Market", status_msg)

                if market_open:
                    console.print(f"\n[bold]{'='*60}[/bold]")
                    console.print(f"[bold cyan]  Run #{self.run_count} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}[/bold cyan]")
                    console.print(f"[bold]{'='*60}[/bold]\n")

                    self.history.save_heartbeat("Active", f"Executing Cycle #{self.run_count}")
                    self.execute_cycle()

                # Calculate sleep time
                interval_seconds = self.interval_hours * 3600
                if hasattr(self, 'broker') and self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        time_to_close = (clock.next_close - clock.timestamp).total_seconds()
                        
                        if not clock.is_open or time_to_close < 1800:
                            # Sleep until exactly 1 minute after the next market open.
                            time_to_open = (clock.next_open - clock.timestamp).total_seconds()
                            if time_to_open > 0:
                                interval_seconds = time_to_open + 60
                    except Exception:
                        pass

                console.print(f"\n[dim]{'─'*60}[/dim]")
                if interval_seconds == self.interval_hours * 3600:
                    next_run_time = (datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)).strftime('%H:%M:%S UTC')
                    console.print(f"[dim]  Next run in {self._format_interval()}. Press Ctrl+C to stop.[/dim]")
                    self.history.save_heartbeat("Sleeping", f"Waiting for next interval. Next run at {next_run_time}")
                else:
                    hours, remainder = divmod(interval_seconds, 3600)
                    minutes = remainder // 60
                    console.print(f"[dim]  Market closed. Sleeping for {int(hours)}h {int(minutes)}m until next open. Press Ctrl+C to stop.[/dim]")
                    self.history.save_heartbeat("Sleeping", f"Market Closed. Sleeping until next open.")
                console.print(f"[dim]{'─'*60}[/dim]\n")

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            console.print("\n[yellow]Scheduler stopped.[/yellow]")
            console.print(f"  Completed {self.run_count} run{'s' if self.run_count != 1 else ''}.")
            storage_type = "Amazon DynamoDB" if os.environ.get("ENV") == "PROD" else f"SQLite (~/.stock_screener/local_history.db)"
            console.print(f"  History: {storage_type}")
            console.print(f"  Run 'python run.py --backtest' to check prediction accuracy.\n")
        finally:
            self.history.close()

    def execute_cycle(self, trigger: str = "SCHEDULED"):
        """Run one full cycle: screen → predict → save → alert → backtest."""
        print(f"[Scheduler] Starting execution cycle (Trigger: {trigger})...")

        # Step 1: Run the full pipeline via ScreenerApp
        # Note: ScreenerApp.run already handles screening, sentiment, technicals, and saving to history.
        # We use it here to ensure identical logic between manual and bot runs.
        predictions, screened_count, alerts = self.app.run(cloud_mode=False, trigger=trigger)

        if not predictions:
            print(f"[Scheduler] No predictions generated in cycle. Ending.")
            return [], 0, []

        # Step 2: Execute trades (Paper Trading)
        # This part is specific to the Scheduler/Bot loop
        print("[Scheduler] Checking for trade execution...")
        if hasattr(self, 'broker') and self.broker:
            self.broker.execute_trades(predictions)
            time.sleep(1)  # Yield to GIL

        # Step 3: Run backtest on old predictions
        if self.run_backtest:
            print("[Scheduler] Running backtester on historical data...")
            self.backtester.run(min_age_days=5)
            time.sleep(1)  # Yield to GIL
            
        print(f"[Bot] Successfully completed cycle #{self.run_count} (Trigger: {trigger})")
        return predictions, screened_count, alerts

    def _format_interval(self) -> str:
        if self.interval_hours >= 24:
            days = self.interval_hours / 24
            if days == 1:
                return "24 hours (daily)"
            elif days == 7:
                return "7 days (weekly)"
            else:
                return f"{days:.0f} days"
        elif self.interval_hours == 0.5:
            return "30 minutes"
        else:
            return f"{self.interval_hours} hours"
