#!/usr/bin/env python3
"""
Stock Screener & Predictor
===========================
Finds stocks under $100 with strong 3-month performance,
fetches this week's news, and predicts movement using
FinBERT sentiment + technical analysis.

Modes:
    python run.py                          # Run screener once
    python run.py --schedule               # Auto-run daily
    python run.py --schedule --every 12    # Auto-run every 12 hours
    python run.py --backtest               # Check past prediction accuracy
    python run.py --alerts                 # Show recent alerts
"""

import argparse

from rich.console import Console

console = Console()


def main():
    parser = argparse.ArgumentParser(
        description="Stock Screener & Predictor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  python run.py                           Run screener once (default)
  python run.py --schedule                Auto-run daily with alerts
  python run.py --schedule --every 12     Auto-run every 12 hours
  python run.py --schedule --every 168    Auto-run weekly
  python run.py --backtest                Check how past predictions performed
  python run.py --alerts                  Show recent alerts

Screener options:
  python run.py --max-price 50            Only stocks under $50
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
        help="Auto-run on a schedule (default: daily)",
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
        "--cloud",
        action="store_true",
        help="Cloud mode: save HTML report to S3 + send email via SES",
    )

    # Screener options
    parser.add_argument(
        "--max-price",
        type=float,
        default=100.0,
        help="Maximum stock price (default: $100)",
    )
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
        default=24.0,
        help="Schedule interval in hours (default: 24 = daily)",
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
    console.print("[bold cyan]║  Find hot stocks under $100 + predict moves ║[/bold cyan]")
    console.print("[bold cyan]║  Powered by FinBERT NLP + Technical Analysis║[/bold cyan]")
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
    elif args.schedule:
        _run_scheduled(args)
    else:
        _run_once(args)


def _run_once(args):
    """Run the screener once."""
    from etf_sentiment.screener_app import ScreenerApp

    app = ScreenerApp(
        max_price=args.max_price,
        min_return=args.min_return,
        top_n=args.top,
    )
    app.run(cloud_mode=args.cloud)


def _run_scheduled(args):
    """Run the screener on a schedule."""
    from etf_sentiment.scheduler import Scheduler

    scheduler = Scheduler(
        max_price=args.max_price,
        min_return=args.min_return,
        top_n=args.top,
        interval_hours=args.every,
        run_backtest=not args.no_backtest,
    )
    scheduler.run()


def _run_backtest():
    """Run backtesting on past predictions."""
    from etf_sentiment.backtester import Backtester

    backtester = Backtester()
    backtester.run()


def _show_alerts():
    """Show recent alerts."""
    from etf_sentiment.alerts import AlertManager

    alert_mgr = AlertManager()
    alert_mgr.show_recent_alerts(hours=72)


if __name__ == "__main__":
    main()
