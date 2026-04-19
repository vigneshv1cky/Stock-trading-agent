"""Dashboard for the stock screener / predictor mode."""

from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _pct(val: float) -> Text:
    color = "green" if val >= 0 else "red"
    return Text(f"{val:+.1f}%", style=color)


def _pred_style(pred: str) -> str:
    if pred == "BULLISH":
        return "bold green"
    elif pred == "BEARISH":
        return "bold red"
    return "yellow"


def _sparkline(closes: list[float], width: int = 20) -> str:
    if not closes or len(closes) < 2:
        return ""
    if len(closes) > width:
        step = len(closes) / width
        closes = [closes[int(i * step)] for i in range(width)]
    blocks = " ▁▂▃▄▅▆▇█"
    mn, mx = min(closes), max(closes)
    rng = mx - mn if mx != mn else 1.0
    going_up = closes[-1] >= closes[0]
    color = "green" if going_up else "red"
    chars = []
    for v in closes:
        idx = int((v - mn) / rng * (len(blocks) - 1))
        chars.append(blocks[idx])
    return f"[{color}]{''.join(chars)}[/{color}]"


def _conf_bar(conf: float) -> str:
    width = 10
    filled = int(conf / 100 * width)
    return "[green]" + "█" * filled + "[dim]" + "░" * (width - filled) + "[/dim]"


class ScreenerDashboard:
    """Renders the stock screener / predictor results."""

    def __init__(self):
        self.console = Console()

    def render(self, predictions: list, screened_count: int):
        self.console.clear()

        # Header
        self.console.print(Panel(
            "[bold cyan]📊 Stock Screener & Predictor[/bold cyan]\n"
            "[dim]Stocks with strong 3-month momentum + this week's news[/dim]",
            border_style="cyan",
        ))

        now = datetime.now(timezone.utc)
        self.console.print(
            f"  [dim]Generated:[/dim] {now.strftime('%Y-%m-%d %H:%M UTC')}"
            f"  [dim]|  Screened:[/dim] {screened_count} stocks passed filters"
            f"  [dim]|  Predictions:[/dim] {len(predictions)}\n"
        )

        if not predictions:
            self.console.print("[yellow]No stocks matched the screening criteria.[/yellow]")
            return

        # Main predictions table
        self.console.print(self._render_predictions_table(predictions))
        self.console.print()

        # Detailed view of top picks
        self.console.print(self._render_top_picks_detail(predictions))
        self.console.print()

        # News summary for top stocks
        self.console.print(self._render_news_summary(predictions))
        self.console.print()

        # Disclaimer
        self.console.print(self._render_disclaimer())

    def _render_predictions_table(self, predictions: list) -> Panel:
        table = Table(
            title="Stock Predictions — Ranked by Overall Score",
            show_header=True,
            header_style="bold cyan",
            border_style="blue",
            expand=True,
        )

        table.add_column("#", min_width=3, justify="right", no_wrap=True)
        table.add_column("Symbol", style="bold", min_width=8, no_wrap=True)
        table.add_column("Price", justify="right", min_width=10, no_wrap=True)
        table.add_column("3M%", justify="right", min_width=8, no_wrap=True)
        table.add_column("1M%", justify="right", min_width=8, no_wrap=True)
        table.add_column("1W%", justify="right", min_width=8, no_wrap=True)
        table.add_column("Prediction", justify="center", min_width=12, no_wrap=True)
        table.add_column("Conf", justify="center", min_width=6, no_wrap=True)
        table.add_column("Score", justify="center", min_width=6, no_wrap=True)
        table.add_column("Mom", justify="center", min_width=5, no_wrap=True)
        table.add_column("Sent", justify="center", min_width=5, no_wrap=True)
        table.add_column("Tech", justify="center", min_width=5, no_wrap=True)
        table.add_column("News", justify="center", min_width=6, no_wrap=True)
        table.add_column("RSI", justify="center", min_width=4, no_wrap=True)
        table.add_column("3M Chart", min_width=24, no_wrap=True)
        table.add_column("Predicted Move", min_width=30, no_wrap=True)

        for i, p in enumerate(predictions, 1):
            score_color = "green" if p.overall_score >= 65 else "red" if p.overall_score < 40 else "yellow"
            rsi_text = Text(f"{p.rsi:.0f}", style="green" if p.rsi and p.rsi < 40 else "red" if p.rsi and p.rsi > 70 else "white") if p.rsi else Text("--", style="dim")
            news_str = f"{p.bullish_count}↑{p.bearish_count}↓"

            table.add_row(
                str(i),
                p.symbol,
                f"${p.current_price:.2f}",
                _pct(p.change_3m_pct),
                _pct(p.change_1m_pct),
                _pct(p.change_1w_pct),
                Text(p.prediction, style=_pred_style(p.prediction)),
                Text(f"{p.confidence:.0f}%", style=score_color),
                Text(f"{p.overall_score:.0f}", style=score_color),
                f"{p.momentum_score:.0f}",
                f"{p.sentiment_score:.0f}",
                f"{p.technical_score:.0f}",
                news_str,
                rsi_text,
                _sparkline(p.sparkline_3m),
                Text(p.predicted_move, style="dim"),
            )

        return Panel(table, border_style="blue")

    def _render_top_picks_detail(self, predictions: list) -> Panel:
        """Show detailed reasoning for top bullish picks."""
        bullish = [p for p in predictions if p.prediction == "BULLISH"]
        if not bullish:
            bullish = predictions[:5]

        lines = []
        for p in bullish[:8]:
            pred_color = _pred_style(p.prediction)
            conf_bar = _conf_bar(p.confidence)

            lines.append(
                f"  [bold]{p.symbol}[/bold] ${p.current_price:.2f} "
                f"[{pred_color}]{p.prediction}[/{pred_color}] "
                f"{conf_bar} {p.confidence:.0f}%"
            )
            lines.append(
                f"    [dim]3M: {p.change_3m_pct:+.1f}%  |  "
                f"1M: {p.change_1m_pct:+.1f}%  |  "
                f"1W: {p.change_1w_pct:+.1f}%  |  "
                f"Range: ${p.low_3m:.2f}-${p.high_3m:.2f}[/dim]"
            )
            lines.append(
                f"    [dim]Predicted: {p.predicted_move}[/dim]"
            )
            for r in p.reasoning:
                lines.append(f"    [dim]• {r}[/dim]")
            lines.append("")

        return Panel(
            "\n".join(lines),
            title="[bold]Top Picks — Detailed Analysis[/bold]",
            border_style="green",
        )

    def _render_news_summary(self, predictions: list) -> Panel:
        """Show this week's top headlines for screened stocks."""
        lines = []
        seen = set()

        for p in predictions:
            for title, score, source, url in p.top_headlines[:2]:
                if title in seen:
                    continue
                seen.add(title)

                if score > 0.1:
                    color = "green"
                elif score < -0.1:
                    color = "red"
                else:
                    color = "yellow"

                lines.append(
                    f"  [{color}]●[/{color}] [{color}]{score:+.2f}[/{color}] "
                    f"[bold]{p.symbol}[/bold] — {title[:75]}\n"
                    f"    [dim]{source}[/dim]"
                )

                if len(lines) >= 30:  # Show up to ~15 headlines (2 lines each)
                    break
            if len(lines) >= 30:
                break

        content = "\n".join(lines) if lines else "[dim]No news articles found for screened stocks this week.[/dim]"
        return Panel(
            content,
            title="[bold]This Week's News for Screened Stocks[/bold]",
            border_style="blue",
        )

    def _render_disclaimer(self) -> Panel:
        return Panel(
            "[bold red]DISCLAIMER:[/bold red] This tool is for [bold]educational "
            "and informational purposes only[/bold]. It does NOT constitute "
            "financial advice. Stock predictions based on sentiment and technicals "
            "are inherently uncertain. Past performance does not guarantee future "
            "results. Always do your own research.",
            border_style="red",
            title="[bold red]⚠ NOT FINANCIAL ADVICE ⚠[/bold red]",
        )
