#!/usr/bin/env python3
"""Real-time multi-agent trading system.

Usage:
    python run_agents.py           # full pipeline — screens, predicts, and trades
    python run_agents.py --dry-run # pipeline without order execution (safe for testing)
"""

import argparse
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-22s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time multi-agent trading bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline but skip all Alpaca order placement",
    )
    args = parser.parse_args()

    from stock_sentiment.agents.orchestrator import Orchestrator

    try:
        asyncio.run(Orchestrator(dry_run=args.dry_run).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
