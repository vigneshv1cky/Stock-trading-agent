#!/usr/bin/env python3
"""
Stock Screener & Predictor
===========================
Finds stocks with strong 3-month performance,
fetches this week's news, and predicts movement using
FinBERT sentiment + technical analysis.

Modes:
    python run.py                          # Run screener once
    python run.py --schedule               # Auto-run every 30 minutes
    python run.py --schedule --every 1     # Auto-run every hour
    python run.py --backtest               # Check past prediction accuracy
    python run.py --alerts                 # Show recent alerts
"""

import argparse
from dotenv import load_dotenv
from rich.console import Console

# Load environment variables from .env file
load_dotenv()

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="Stock Screener & Predictor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  python run.py                           Run screener once (default)
  python run.py --schedule                Auto-run every 30 mins with alerts
  python run.py --schedule --every 1      Auto-run every hour
  python run.py --schedule --every 24     Auto-run daily
  python run.py --backtest                Check how past predictions performed
  python run.py --alerts                  Show recent alerts

Screener options:
  python run.py --min-return 25           Only stocks up >25% in 3 months
  python run.py --top 50                  Show top 50 results

DISCLAIMER: This tool is for educational purposes only.
It does NOT constitute financial advice.
        """,
    )

    # Mode flags
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Auto-run on a schedule (default: 30 minutes)",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Check how past predictions actually performed",
    )
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="Show recent alerts from past runs",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Fit scoring weights from backtest outcomes (needs 50+ checked predictions)",
    )
    # Screener options
    parser.add_argument(
        "--min-return",
        type=float,
        default=10.0,
        help="Minimum 3-month return %% (default: 10%%)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="Number of top stocks to show (default: 30)",
    )

    # Schedule options
    parser.add_argument(
        "--every",
        type=float,
        default=0.5,
        help="Schedule interval in hours (default: 0.5 = 30 minutes)",
    )
    parser.add_argument(
        "--no-backtest",
        action="store_true",
        help="Disable auto-backtest during scheduled runs",
    )

    args = parser.parse_args()

    # Print banner
    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║     📊 Stock Screener & Predictor           ║[/bold cyan]")
    console.print("[bold cyan]║  Find hot stocks + predict moves            ║[/bold cyan]")
    console.print("[bold cyan]║  Powered by Claude AI + Technical Analysis  ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════════════╝[/bold cyan]")
    console.print()
    console.print(
        "[bold red]⚠  DISCLAIMER: This is NOT financial advice. "
        "Use at your own risk.[/bold red]\n"
    )

    if args.backtest:
        _run_backtest()
    elif args.alerts:
        _show_alerts()
    elif args.optimize:
        _run_optimize()
    elif args.schedule:
        _run_scheduled(args)
    else:
        _run_once(args)


def _run_once(args):
    """Run the screener once."""
    from stock_sentiment.screener_app import ScreenerApp

    app = ScreenerApp(
        min_return=args.min_return,
        top_n=args.top,
    )
    app.run(trigger="CLI")


def _run_scheduled(args):
    """Run the screener on a schedule."""
    from stock_sentiment.scheduler import Scheduler

    scheduler = Scheduler(
        min_return=args.min_return,
        top_n=args.top,
        interval_hours=args.every,
        run_backtest=not args.no_backtest,
    )
    scheduler.run()


def _run_backtest():
    """Run backtesting on past predictions."""
    from stock_sentiment.backtester import Backtester

    backtester = Backtester()
    backtester.run()


def _show_alerts():
    """Show recent alerts."""
    from stock_sentiment.alerts import AlertManager

    alert_mgr = AlertManager()
    alert_mgr.show_recent_alerts(hours=72)


def _run_optimize():
    """Fit scoring weights from backtest outcomes."""
    from stock_sentiment.market.weight_optimizer import WeightOptimizer
    from stock_sentiment.history import History

    history = History()
    try:
        WeightOptimizer(history).optimize()
    finally:
        history.close()


if __name__ == "__main__":
    main()
