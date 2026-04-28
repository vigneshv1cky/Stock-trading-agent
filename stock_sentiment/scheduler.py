"""Scheduler: auto-run the screener every 15 minutes during market hours."""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _ET = _ZoneInfo("America/New_York")
except ImportError:
    _ET = timezone(timedelta(hours=-4))  # type: ignore[assignment]

from rich.console import Console

from stock_sentiment.alerts import AlertManager
from stock_sentiment.backtester import Backtester
from stock_sentiment.history import History
from stock_sentiment.market.broker import PaperBroker
from stock_sentiment.screener_app import ScreenerApp

console = Console()

# Shared lock — background scheduler holds this during execute_cycle(); force-trade waits on it.
_cycle_lock = threading.Lock()


class Scheduler:
    """Runs the full screen → predict → trade pipeline every 15 minutes."""

    def __init__(
        self,
        top_n: int = 40,
        interval_hours: float = 0.25,
        run_backtest: bool = True,
        min_return: float = 10.0,
    ):
        self.app = ScreenerApp(top_n=top_n, min_return=min_return)
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
        console.print("  Strategy: Brain-Only (Institutional Filters)")

        storage_type = (
            "Amazon DynamoDB"
            if os.environ.get("ENV") == "PROD"
            else "SQLite (~/.stock_screener/local_history.db)"
        )
        console.print(f"  History:  {storage_type}")
        console.print()

        try:
            while True:
                time.sleep(1)
                self.run_count += 1
                now = datetime.now(_ET)

                market_open = True
                status_msg = "Market Open - Initializing Cycle"
                clock = None

                if self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        market_open = clock.is_open
                        time_to_open = (clock.next_open - clock.timestamp).total_seconds()

                        if market_open:
                            time_to_close = (clock.next_close - clock.timestamp).total_seconds()
                            mins_to_close = int(time_to_close // 60)
                            if time_to_close < 900:
                                market_open = False
                                status_msg = "Market Closing Soon - Skipping"
                                console.print(
                                    f"[yellow]⏰  Market closes in {mins_to_close}m — skipping cycle.[/yellow]"
                                )
                        elif 0 < time_to_open <= 900:
                            market_open = True
                            status_msg = "Pre-Market Open - Preparing Orders"
                            console.print(
                                f"[cyan]🔔  Pre-market window ({int(time_to_open//60)}m to open) — preparing orders.[/cyan]"
                            )

                        if not market_open and "Closing" not in status_msg:
                            next_open = clock.next_open.strftime("%H:%M ET")
                            status_msg = f"Market Closed until {next_open}"
                    except Exception as e:
                        console.print(f"[yellow]⚠  Market check failed: {e}[/yellow]")

                self.history.save_heartbeat("Checking Market", status_msg)

                if market_open:
                    console.print(f"\n[bold]{'─' * 60}[/bold]")
                    console.print(
                        f"[bold cyan]  Cycle #{self.run_count}[/bold cyan]"
                        f"  [dim]{now.strftime('%Y-%m-%d %H:%M ET')}[/dim]"
                        + (
                            f"  [dim]· closes in {int((clock.next_close - clock.timestamp).total_seconds() // 60)}m[/dim]"
                            if clock and clock.is_open else ""
                        )
                    )
                    console.print(f"[bold]{'─' * 60}[/bold]\n")

                    self.history.save_heartbeat("Active", f"Executing Cycle #{self.run_count}")
                    with _cycle_lock:
                        self.execute_cycle(trigger="BOT SCAN")

                # Calculate sleep time
                interval_seconds = self.interval_hours * 3600
                if self.broker and self.broker.client:
                    try:
                        clock = self.broker.client.get_clock()
                        time_to_open = (clock.next_open - clock.timestamp).total_seconds()
                        if not clock.is_open or (clock.next_close - clock.timestamp).total_seconds() < 900:
                            if time_to_open > 900:
                                interval_seconds = time_to_open - 900
                            elif time_to_open > 0:
                                interval_seconds = time_to_open + 60
                    except Exception:
                        pass

                next_run_dt = datetime.now(_ET) + timedelta(seconds=interval_seconds)
                mins = int(interval_seconds // 60)

                if interval_seconds == self.interval_hours * 3600:
                    console.print(
                        f"\n[dim]  💤  Next cycle in {mins}m"
                        f" ({next_run_dt.strftime('%H:%M ET')})[/dim]"
                    )
                    self.history.save_heartbeat("Sleeping", f"Standard Sleep|{next_run_dt.isoformat()}")
                else:
                    hrs = mins // 60
                    rem = mins % 60
                    duration_str = f"{hrs}h {rem}m" if hrs else f"{mins}m"
                    console.print(
                        f"\n[dim]  🌙  Market closed — deep sleep {duration_str}"
                        f" (opens ~{next_run_dt.strftime('%H:%M ET')})[/dim]"
                    )
                    self.history.save_heartbeat("Sleeping", f"Market Closed|{next_run_dt.isoformat()}")

                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            console.print("\n[yellow]Scheduler stopped.[/yellow]")
        finally:
            self.history.close()

    def execute_cycle(self, trigger: str = "BOT SCAN"):
        """Run one full cycle: screen → predict → trade → alert → backtest."""
        print(f"[Scheduler] Starting execution cycle (Trigger: {trigger})...")

        predictions, screened_count, alerts = self.app.run(trigger=trigger)

        if not predictions:
            return [], 0, []

        # NOTE: broker.execute_trades is called inside app.run() — do not call again here.

        if self.run_backtest:
            print("[Scheduler] Running Backtester...")
            self.backtester.run(min_age_days=5)

        print(f"[Bot] Successfully completed cycle #{self.run_count}")
        return predictions, screened_count, alerts

    def _format_interval(self) -> str:
        mins = int(self.interval_hours * 60)
        if self.interval_hours >= 24:
            return f"{self.interval_hours / 24:.0f} days"
        if self.interval_hours >= 1:
            return f"{self.interval_hours:.0f} hours"
        return f"{mins} minutes"
