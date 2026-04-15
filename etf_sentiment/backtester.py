"""Backtester: checks how past predictions actually performed.

Looks at predictions made 5+ days ago, fetches what the stock
actually did, and reports accuracy.
"""

from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from etf_sentiment.history import History
from etf_sentiment.market.price_fetcher import PriceFetcher

console = Console()


class Backtester:
    """Checks past predictions against actual outcomes."""

    def __init__(self, history: History = None):
        self.history = history or History()
        self.price_fetcher = PriceFetcher(cache_ttl_seconds=300)

    def run(self, min_age_days: int = 5):
        """Check all predictions old enough and report results."""
        console.print("\n[bold cyan]Running Backtest...[/bold cyan]\n")

        # Find predictions that haven't been checked yet
        unchecked = self.history.get_predictions_needing_backtest(min_age_days)

        if not unchecked:
            console.print("[yellow]No predictions old enough to backtest (need 5+ days).[/yellow]")
            console.print("[dim]Run the screener a few times, wait a week, then backtest.[/dim]\n")
            self._show_existing_stats()
            return

        console.print(f"  Found {len(unchecked)} predictions to check...")

        # Fetch current prices for all symbols
        symbols = list(set(p["symbol"] for p in unchecked))
        console.print(f"  Fetching prices for {len(symbols)} symbols...")

        import yfinance as yf
        # We need historical data from prediction date to now
        prices_data = yf.download(
            symbols,
            period="1mo",
            group_by="ticker" if len(symbols) > 1 else "column",
            progress=False,
            threads=True,
        )

        checked = 0
        for pred in unchecked:
            try:
                symbol = pred["symbol"]
                pred_price = pred["price_at_prediction"]
                pred_date = pred["predicted_at"][:10]  # YYYY-MM-DD

                if len(symbols) == 1:
                    df = prices_data
                else:
                    df = prices_data.get(symbol)

                if df is None or (hasattr(df, 'empty') and df.empty):
                    continue

                import pandas as pd
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                closes = df["Close"].astype(float)
                dates = [str(d)[:10] for d in closes.index]

                # Find the prediction date index
                pred_idx = None
                for i, d in enumerate(dates):
                    if d >= pred_date:
                        pred_idx = i
                        break

                if pred_idx is None:
                    continue

                # Get prices at 1d, 3d, 5d, 10d after prediction
                prices = {}
                for label, offset in [("1d", 1), ("3d", 3), ("5d", 5), ("10d", 10)]:
                    idx = pred_idx + offset
                    if idx < len(closes):
                        p = float(closes.iloc[idx])
                        prices[label] = p
                        prices[f"ret_{label}"] = ((p - pred_price) / pred_price) * 100
                    else:
                        prices[label] = None
                        prices[f"ret_{label}"] = None

                # Was the prediction correct?
                # BULLISH = stock went up; BEARISH = stock went down
                ret_5d = prices.get("ret_5d") or prices.get("ret_3d") or prices.get("ret_1d")
                if ret_5d is not None:
                    if pred["prediction"] == "BULLISH":
                        correct = ret_5d > 0
                    elif pred["prediction"] == "BEARISH":
                        correct = ret_5d < 0
                    else:  # NEUTRAL
                        correct = abs(ret_5d) < 3  # Within 3% counts as correct for NEUTRAL
                else:
                    correct = None

                if correct is not None:
                    self.history.save_outcome(
                        pred["id"], symbol, pred_price, prices, correct
                    )
                    checked += 1

            except Exception:
                continue

        console.print(f"  [green]Checked {checked} predictions[/green]\n")
        self._show_results()

    def _show_existing_stats(self):
        """Show stats from previously checked predictions."""
        stats = self.history.get_backtest_stats()
        if stats["total"] == 0:
            return

        console.print(self._render_stats(stats))
        console.print()
        console.print(self._render_outcomes_table())

    def _show_results(self):
        """Show full backtest results."""
        stats = self.history.get_backtest_stats()
        console.print(self._render_stats(stats))
        console.print()
        console.print(self._render_outcomes_table())

    def _render_stats(self, stats: dict) -> Panel:
        """Render aggregate backtest statistics."""
        if stats["total"] == 0:
            return Panel("[dim]No backtest data yet.[/dim]", title="Backtest Stats")

        accuracy_color = "green" if stats["accuracy"] > 0.6 else "yellow" if stats["accuracy"] > 0.4 else "red"
        bullish_acc_color = "green" if stats["bullish_accuracy"] > 0.6 else "yellow" if stats["bullish_accuracy"] > 0.4 else "red"

        def fmt_ret(r):
            if r is None:
                return "--"
            color = "green" if r > 0 else "red"
            return f"[{color}]{r:+.2f}%[/{color}]"

        lines = [
            f"  Total predictions tested: [bold]{stats['total']}[/bold]",
            f"  Overall accuracy:         [{accuracy_color}][bold]{stats['accuracy']:.1%}[/bold][/{accuracy_color}] ({stats['correct']}/{stats['total']})",
            f"  Bullish accuracy:         [{bullish_acc_color}][bold]{stats['bullish_accuracy']:.1%}[/bold][/{bullish_acc_color}] ({stats['bullish_total']} bullish predictions)",
            f"",
            f"  Avg return after predictions:",
            f"    1 day:  {fmt_ret(stats['avg_return_1d'])}",
            f"    3 days: {fmt_ret(stats['avg_return_3d'])}",
            f"    5 days: {fmt_ret(stats['avg_return_5d'])}",
            f"   10 days: {fmt_ret(stats['avg_return_10d'])}",
        ]

        return Panel(
            "\n".join(lines),
            title="[bold]Backtest Results Summary[/bold]",
            border_style="cyan",
        )

    def _render_outcomes_table(self) -> Panel:
        """Render recent individual outcomes."""
        outcomes = self.history.get_outcomes(limit=30)
        if not outcomes:
            return Panel("[dim]No individual outcomes to show.[/dim]", title="Outcomes")

        table = Table(
            title="Individual Prediction Outcomes",
            show_header=True,
            header_style="bold cyan",
            border_style="blue",
            expand=True,
        )

        table.add_column("Symbol", style="bold", min_width=7, no_wrap=True)
        table.add_column("Prediction", justify="center", min_width=10, no_wrap=True)
        table.add_column("Conf", justify="center", min_width=6, no_wrap=True)
        table.add_column("Entry $", justify="right", min_width=9, no_wrap=True)
        table.add_column("1D Ret", justify="right", min_width=8, no_wrap=True)
        table.add_column("3D Ret", justify="right", min_width=8, no_wrap=True)
        table.add_column("5D Ret", justify="right", min_width=8, no_wrap=True)
        table.add_column("10D Ret", justify="right", min_width=8, no_wrap=True)
        table.add_column("Correct?", justify="center", min_width=9, no_wrap=True)
        table.add_column("Predicted Move", min_width=20, no_wrap=True)

        for o in outcomes:
            pred_style = "green" if o["prediction"] == "BULLISH" else "red" if o["prediction"] == "BEARISH" else "yellow"

            def fmt_ret(r):
                if r is None:
                    return Text("--", style="dim")
                return Text(f"{r:+.2f}%", style="green" if r > 0 else "red")

            correct_text = Text(
                "✓ YES" if o["prediction_correct"] else "✗ NO",
                style="bold green" if o["prediction_correct"] else "bold red",
            )

            table.add_row(
                o["symbol"],
                Text(o["prediction"], style=pred_style),
                f"{o['confidence']:.0f}%",
                f"${o['price_at_prediction']:.2f}",
                fmt_ret(o["return_1d_pct"]),
                fmt_ret(o["return_3d_pct"]),
                fmt_ret(o["return_5d_pct"]),
                fmt_ret(o["return_10d_pct"]),
                correct_text,
                Text(o.get("predicted_move", "")[:18] or "", style="dim"),
            )

        return Panel(table, border_style="blue")
