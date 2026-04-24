"""Scheduler: auto-run the screener on a daily/weekly schedule.

Runs the full screener pipeline on a loop, saves results to history,
checks alerts, and optionally runs backtests.
"""

import time
from datetime import datetime, timezone, timedelta
import os
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
        top_n: int = 40,
        interval_hours: float = 0.5,
        run_backtest: bool = True,
    ):
        self.app = ScreenerApp(top_n=top_n)
        self.history = History()
        self.alerts = AlertManager(self.history, disable_notifications=True)
        self.backtester = Backtester(self.history)
        self.broker = PaperBroker()
        self.interval_hours = interval_hours
        self.run_backtest = run_backtest
        self.top_n = top_n
        self.run_count = 0

    def run(self):
        """Main scheduling loop."""
        console.print("\n[bold cyan]Starting AI Trading Bot[/bold cyan]\n")
        console.print(f"  Schedule: Every {self._format_interval()}")
        console.print(f"  Scan Depth: Top {self.top_n} active stocks")
        console.print(f"  Strategy: Brain-Only (Institutional Filters)")
        
        storage_type = "Amazon DynamoDB" if os.environ.get("ENV") == "PROD" else f"SQLite (~/.stock_screener/local_history.db)"
        console.print(f"  History:  {storage_type}")
        console.print()

        try:
            while True:
                time.sleep(1)
                self.run_count += 1
                now = datetime.now(timezone.utc)
                
                print(f"[Brain] Step 1: Market Check... (Run #{self.run_count})")
                market_open = True
                status_msg = "Market Open - Initializing Cycle"
                
                if hasattr(self, 'broker') and self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        market_open = clock.is_open
                        time_to_open = (clock.next_open - clock.timestamp).total_seconds()
                        
                        if market_open:
                            time_to_close = (clock.next_close - clock.timestamp).total_seconds()
                            if time_to_close < 1800:
                                market_open = False
                                status_msg = "Market Closing Soon - Skipping"
                        elif time_to_open > 0 and time_to_open <= 900:
                            # PRE-MARKET: Wake up 15 minutes before the bell
                            market_open = True
                            status_msg = "Pre-Market Open - Preparing Orders"
                            print("[Brain] PRE-MARKET window detected. Waking up to prepare orders.")
                            
                        if not market_open:
                            next_open = clock.next_open.strftime('%Y-%m-%d %H:%M:%S UTC')
                            status_msg = f"Market Closed until {next_open}"
                            print(f"[Brain] Step 2: Market CLOSED. Flowing to SLEEP.")
                    except Exception as e:
                        console.print(f"[yellow]Warning: Market check failed: {e}[/yellow]")

                self.history.save_heartbeat("Checking Market", status_msg)

                if market_open:
                    print(f"[Brain] Step 2: Market OPEN (or Pre-Market). Initiating Core Pipeline.")
                    console.print(f"\n[bold]{'='*60}[/bold]")
                    console.print(f"[bold cyan]  Run #{self.run_count} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}[/bold cyan]")
                    console.print(f"[bold]{'='*60}[/bold]\n")

                    self.history.save_heartbeat("Active", f"Executing Cycle #{self.run_count}")
                    self.execute_cycle(trigger="BOT SCAN")

                # Calculate sleep time
                interval_seconds = self.interval_hours * 3600
                if hasattr(self, 'broker') and self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        if not clock.is_open or (clock.next_close - clock.timestamp).total_seconds() < 1800:
                            time_to_open = (clock.next_open - clock.timestamp).total_seconds()
                            if time_to_open > 900:
                                # Sleep until exactly 15 minutes before market open
                                interval_seconds = time_to_open - 900
                            elif time_to_open > 0:
                                # In pre-market window: sleep until 1 minute after the bell rings
                                interval_seconds = time_to_open + 60
                    except Exception: pass

                next_run_dt = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)

                if interval_seconds == self.interval_hours * 3600:
                    print(f"[Brain] Step 3: Cycle complete. Sleeping until {next_run_dt.strftime('%H:%M:%S UTC')}.")
                    self.history.save_heartbeat("Sleeping", f"Standard Sleep|{next_run_dt.isoformat()}")
                else:
                    print(f"[Brain] Step 3: Market closed. Deep Sleep initiated.")
                    self.history.save_heartbeat("Sleeping", f"Market Closed|{next_run_dt.isoformat()}")

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            console.print("\n[yellow]Scheduler stopped.[/yellow]")
        finally:
            self.history.close()

    def execute_cycle(self, trigger: str = "BOT SCAN"):
        """Run one full cycle: screen → predict → trade → alert → backtest."""
        print(f"[Scheduler] Starting execution cycle (Trigger: {trigger})...")
        
        # In production, enable cloud_mode to generate S3 reports and send emails
        is_prod = os.environ.get("ENV") == "PROD"
        predictions, screened_count, alerts = self.app.run(cloud_mode=is_prod, trigger=trigger)

        if not predictions:
            return [], 0, []

        print("[Scheduler] Checking Trade Execution...")
        if hasattr(self, 'broker') and self.broker:
            self.broker.execute_trades(predictions)

        if self.run_backtest:
            print("[Scheduler] Running Backtester...")
            self.backtester.run(min_age_days=5)
            
        print(f"[Bot] Successfully completed cycle #{self.run_count}")
        return predictions, screened_count, alerts

    def _format_interval(self) -> str:
        if self.interval_hours >= 24: return f"{self.interval_hours / 24:.0f} days"
        return "30 minutes" if self.interval_hours == 0.5 else f"{self.interval_hours} hours"
