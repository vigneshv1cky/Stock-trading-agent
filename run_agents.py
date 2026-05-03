#!/usr/bin/env python3
"""Real-time multi-agent trading system.

Usage:
    python run_agents.py           # full pipeline — screens, predicts, and trades
    python run_agents.py --dry-run # pipeline without order execution (safe for testing)
"""

import argparse
import asyncio
import logging
import re
import sys

from dotenv import load_dotenv

load_dotenv()

# ── ANSI codes ────────────────────────────────────────────────────────────────
_R  = "\033[0m"
_B  = "\033[1m"
_DIM = "\033[2m"

_RED    = "\033[31m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_BLUE   = "\033[34m"
_MAG    = "\033[35m"
_CYAN   = "\033[36m"
_WHITE  = "\033[37m"

_BRED   = "\033[91m"
_BGREEN = "\033[92m"
_BYEL   = "\033[93m"
_BBLUE  = "\033[94m"
_BMAG   = "\033[95m"
_BCYAN  = "\033[96m"

# Per-agent identity colors
_AGENT_COLOR: dict[str, str] = {
    "WatcherAgent":       _CYAN,
    "CryptoWatcherAgent": _BCYAN,
    "ScannerAgent":       _BLUE,
    "ScreenerAgent":      _MAG,
    "ResearchAgent":      _WHITE,
    "NewsWatcherAgent":   _GREEN,
    "NewsAgent":          _YELLOW,
    "PredictorAgent":     _BBLUE,
    "RiskAgent":          _BYEL,
    "ExecutorAgent":      _BGREEN,
    "LearningAgent":      _BMAG,
    "MonitorAgent":       _CYAN,
    "PortfolioAgent":     _DIM,
    "Orchestrator":       f"{_B}{_WHITE}",
}

# Keywords to highlight inside the message body
_HIGHLIGHTS: list[tuple[str, str]] = [
    (r"\bBUY\b",              f"{_B}{_BGREEN}"),
    (r"\bSHORT\b",            f"{_B}{_BRED}"),
    (r"\bCLOSE\b",            f"{_B}{_YELLOW}"),
    (r"\bAPPROVED?\b",        _BGREEN),
    (r"\bBLOCKED?\b",         _RED),
    (r"\bBULLISH\b",          _BGREEN),
    (r"\bBEARISH\b",          _BRED),
    (r"\bFATAL\b",            f"{_B}{_BRED}"),
    (r"\bFAILED\b",           _BRED),
    (r"\bCrashed?\b",         _BRED),
    (r"\bCancelled?\b",       _YELLOW),
    (r"\bEOD\b",              _BYEL),
    (r"\bPredicted:\s+\S+",   _BBLUE),
]


def _hilite(color: str):
    def _replace(m: re.Match[str]) -> str:
        return f"{color}{m.group()}{_R}"
    return _replace


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts   = f"{_DIM}{self.formatTime(record, '%H:%M:%S')}{_R}"
        nc   = _AGENT_COLOR.get(record.name, _DIM)
        name = f"{nc}{record.name:<20}{_R}"

        lvl = record.levelname
        if lvl == "WARNING":
            badge = f"{_YELLOW} ⚠  {_R}"
        elif lvl in ("ERROR", "CRITICAL"):
            badge = f"{_BRED} ✗  {_R}"
        else:
            badge = "    "

        msg = record.getMessage()
        for pat, color in _HIGHLIGHTS:
            msg = re.sub(pat, _hilite(color), msg)

        line = f"{ts}  {name}  {badge}{msg}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += f"\n{_DIM}{record.exc_text}{_R}"

        return line


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if sys.stdout.isatty():
        handler.setFormatter(_ColorFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
            datefmt="%H:%M:%S",
        ))
    logging.root.setLevel(logging.INFO)
    logging.root.handlers = [handler]

    # Silence noisy third-party libraries
    for noisy in (
        "alpaca",
        "alpaca.data.live.websocket",
        "alpaca.trading",
        "botocore",
        "boto3",
        "urllib3",
        "websockets",
        "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print_banner(dry_run: bool) -> None:
    if not sys.stdout.isatty():
        return
    mode  = f"{_BYEL}DRY-RUN{_R}" if dry_run else f"{_BGREEN}LIVE{_R}"
    line  = f"{_B}{_BCYAN}{'─' * 48}{_R}"
    print(f"\n{line}")
    print(f"  {_B}{_WHITE}TRADING BOT{_R}  ·  {mode}")
    print(f"{line}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time multi-agent trading bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but skip all Alpaca order placement",
    )
    args = parser.parse_args()

    _setup_logging()
    _print_banner(args.dry_run)

    from stock_sentiment.agents.orchestrator import Orchestrator

    try:
        asyncio.run(Orchestrator(dry_run=args.dry_run).run())
    except KeyboardInterrupt:
        print(f"\n{_DIM}Shutting down…{_R}\n")


if __name__ == "__main__":
    main()
