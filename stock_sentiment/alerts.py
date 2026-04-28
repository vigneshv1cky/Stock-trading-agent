"""Alert system: detects new stocks, BULLISH flips, and score changes.

Compares current run against previous run to generate alerts.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from stock_sentiment.history import BaseStorage, History

console = Console()


class AlertManager:
    """Detects and delivers alerts based on prediction changes."""

    def __init__(self, history: BaseStorage | None = None, disable_notifications: bool = False):
        self.history = history or History()
        # disable_notifications kept for call-site compat; desktop notifications removed

    def check_and_alert(self, current_predictions: list) -> list[dict]:
        """Compare current predictions against previous run and generate alerts.

        Alert types:
        - NEW_ENTRY: Stock newly appeared in the screener
        - BULLISH_FLIP: Stock changed from NEUTRAL/BEARISH to BULLISH
        - SCORE_SURGE: Overall score increased significantly (>15 points)
        - HIGH_CONFIDENCE: BULLISH prediction with overall score >= 80
        """
        alerts = []

        try:
            # Get previous run's data
            prev_symbols = self.history.get_all_symbols_from_last_run()
            last_run = self.history.get_latest_run()

            for pred in current_predictions:
                symbol = pred.symbol

                # NEW_ENTRY: stock wasn't in the last run
                if symbol not in prev_symbols and prev_symbols:
                    alert = {
                        "alert_type": "NEW_ENTRY",
                        "symbol": symbol,
                        "message": f"{symbol} just entered the screener at ${pred.current_price:.2f} "
                                   f"(3M: +{pred.change_3m_pct:.1f}%, Score: {pred.overall_score:.0f})",
                        "prediction": pred.prediction,
                        "score": pred.overall_score,
                        "price": pred.current_price,
                    }
                    alerts.append(alert)
                    self.history.save_alert(**alert)

                # Check for flips and surges if we have previous data
                if last_run:
                    run_id = str(last_run.get("id") or last_run.get("run_id") or "")
                    prev = self.history.get_prediction_by_symbol_and_run(symbol, run_id)
                    if prev:
                        # BULLISH_FLIP
                        if pred.prediction == "BULLISH" and prev.get("prediction") != "BULLISH":
                            alert = {
                                "alert_type": "BULLISH_FLIP",
                                "symbol": symbol,
                                "message": f"{symbol} flipped to BULLISH from {prev.get('prediction')} "
                                           f"(Score: {float(prev.get('overall_score', 0)):.0f} → {pred.overall_score:.0f})",
                                "prediction": pred.prediction,
                                "score": pred.overall_score,
                                "price": pred.current_price,
                            }
                            alerts.append(alert)
                            self.history.save_alert(**alert)

                        # SCORE_SURGE
                        score_change = pred.overall_score - float(prev.get("overall_score", 0))
                        if score_change >= 15:
                            alert = {
                                "alert_type": "SCORE_SURGE",
                                "symbol": symbol,
                                "message": f"{symbol} score surged +{score_change:.0f} points "
                                           f"({float(prev.get('overall_score', 0)):.0f} → {pred.overall_score:.0f})",
                                "prediction": pred.prediction,
                                "score": pred.overall_score,
                                "price": pred.current_price,
                            }
                            alerts.append(alert)
                            self.history.save_alert(**alert)

                # HIGH_CONFIDENCE: always alert on very strong signals
                if pred.prediction == "BULLISH" and pred.overall_score >= 80:
                    alert = {
                        "alert_type": "HIGH_CONFIDENCE",
                        "symbol": symbol,
                        "message": f"{symbol} BULLISH with extreme conviction "
                                   f"(Score: {pred.overall_score:.0f}/100, Price: ${pred.current_price:.2f})",
                        "prediction": pred.prediction,
                        "score": pred.overall_score,
                        "price": pred.current_price,
                    }
                    alerts.append(alert)
                    self.history.save_alert(**alert)

        except Exception as e:
            print(f"[AlertManager] Error generating alerts: {e}")

        return alerts

    def display_alerts(self, alerts: list[dict]):
        """Show alerts in the terminal."""
        if not alerts:
            return

        console.print(self._render_alerts_panel(alerts))
        console.print()

    def show_recent_alerts(self, hours: int = 24):
        """Show alerts from the last N hours."""
        if not hasattr(self.history, 'get_recent_alerts'):
            return

        alerts = self.history.get_recent_alerts(hours)
        if not alerts:
            console.print("[dim]No alerts in the last 24 hours.[/dim]")
            return

        table = Table(
            title=f"Alerts (Last {hours} hours)",
            show_header=True,
            header_style="bold cyan",
            border_style="yellow",
            expand=True,
        )

        table.add_column("Time", min_width=18, no_wrap=True)
        table.add_column("Type", min_width=14, no_wrap=True)
        table.add_column("Symbol", style="bold", min_width=7, no_wrap=True)
        table.add_column("Message", min_width=50)

        for a in alerts:
            type_colors = {
                "NEW_ENTRY": "cyan",
                "BULLISH_FLIP": "bold green",
                "SCORE_SURGE": "bold yellow",
                "HIGH_CONFIDENCE": "bold green",
                "NEWS_FEED_DEAD": "bold red",
                "NEWS_FEED_DEGRADED": "bold yellow",
            }
            color = type_colors.get(a["alert_type"], "white")

            table.add_row(
                a["created_at"][:16],
                Text(a["alert_type"], style=color),
                a["symbol"],
                a["message"],
            )

        console.print(Panel(table, border_style="yellow"))

    def _render_alerts_panel(self, alerts: list[dict]) -> Panel:
        """Render alerts as a rich panel."""
        icons = {
            "NEW_ENTRY": "🆕",
            "BULLISH_FLIP": "🔄",
            "SCORE_SURGE": "📈",
            "HIGH_CONFIDENCE": "🎯",
            "NEWS_FEED_DEAD": "💀",
            "NEWS_FEED_DEGRADED": "⚠",
        }
        colors = {
            "NEW_ENTRY": "cyan",
            "BULLISH_FLIP": "bold green",
            "SCORE_SURGE": "bold yellow",
            "HIGH_CONFIDENCE": "bold green",
            "NEWS_FEED_DEAD": "bold red",
            "NEWS_FEED_DEGRADED": "bold yellow",
        }

        lines = []
        for a in alerts:
            atype = a["alert_type"]
            icon = icons.get(atype, "•")
            color = colors.get(atype, "white")
            lines.append(f"  {icon} [{color}]{atype}[/{color}] — {a['message']}")

        return Panel(
            "\n".join(lines),
            title=f"[bold yellow]⚡ {len(alerts)} Alert{'s' if len(alerts) != 1 else ''}[/bold yellow]",
            border_style="yellow",
        )
