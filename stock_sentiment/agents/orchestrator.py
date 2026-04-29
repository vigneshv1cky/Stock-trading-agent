"""Orchestrator — creates all agents, wires them to the shared EventBus, and runs them.

Usage:
    asyncio.run(Orchestrator().run())
    asyncio.run(Orchestrator(dry_run=True).run())   # no trade execution

Agent pipeline:
    MacroAgent          → macro.context        (market regime, every 5 min)
    WatcherAgent        → market.signal        (RVOL + price gate, reactive)
    ScannerAgent        → market.signal        (batch scan every 15 min, proactive)
    ScreenerAgent       → symbol.screened      (qualification + archetype)
    ResearchAgent       → symbol.researched    (options, technicals, short interest)
    NewsAgent           → symbol.analysed      (RSS + Bedrock sentiment)
    PredictorAgent      → symbol.predicted     (Sonnet + extended thinking)
    CriticAgent         → symbol.reviewed      (two-turn adversarial debate)
    PortfolioAgent      → portfolio.state      (sector concentration tracking)
    RiskAgent           → trade.approved       (VIX + concentration + cooldown)
    ExecutorAgent       → trade.executed / trade.closed
    LearningAgent       → memory.updated       (Sonnet reflection, per-archetype)
    MonitorAgent        → position.alert       (earnings + re-eval)
"""

import asyncio
import logging
import signal

from stock_sentiment.market.screener import SCREEN_UNIVERSE

from .critic import CriticAgent
from .crypto_watcher import CRYPTO_UNIVERSE, CryptoWatcherAgent
from .event_bus import EventBus
from .executor import ExecutorAgent
from .learning import LearningAgent
from .macro import MacroAgent
from .memory import AgentMemory
from .monitor import MonitorAgent
from .news import NewsAgent
from .portfolio import PortfolioAgent
from .predictor import PredictorAgent
from .research import ResearchAgent
from .risk import RiskAgent
from .scanner import ScannerAgent
from .screener import ScreenerAgent
from .watcher import WatcherAgent

log = logging.getLogger("Orchestrator")


class Orchestrator:
    def __init__(self, dry_run: bool = False, mode: str = "stocks"):
        self.dry_run = dry_run
        self.mode = mode  # "stocks", "crypto", "both"
        self.bus = EventBus()
        self.memory = AgentMemory()
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    async def run(self) -> None:
        stock_symbols = list(dict.fromkeys(SCREEN_UNIVERSE))

        from .base import BaseAgent
        agents: list[BaseAgent] = [MacroAgent(self.bus)]  # must start first

        if self.mode in ("stocks", "both"):
            agents += [
                WatcherAgent(self.bus, stock_symbols),
                ScannerAgent(self.bus, stock_symbols),
            ]

        if self.mode in ("crypto", "both"):
            agents.append(CryptoWatcherAgent(self.bus))

        agents += [
            ScreenerAgent(self.bus),
            ResearchAgent(self.bus),
            NewsAgent(self.bus),
            PredictorAgent(self.bus, self.memory),
            CriticAgent(self.bus, self.memory),
            PortfolioAgent(self.bus),
            RiskAgent(self.bus),
            ExecutorAgent(self.bus, dry_run=self.dry_run, mode=self.mode),
            LearningAgent(self.bus, self.memory),
            MonitorAgent(self.bus),
        ]

        self._tasks = [
            asyncio.create_task(agent.safe_run(), name=agent.name)
            for agent in agents
        ]

        symbols_count = (
            len(stock_symbols) if self.mode == "stocks"
            else len(CRYPTO_UNIVERSE) if self.mode == "crypto"
            else len(stock_symbols) + len(CRYPTO_UNIVERSE)
        )
        run_mode = "DRY-RUN" if self.dry_run else "LIVE"
        log.info(
            "Multi-agent trading system started [%s] mode=%s — %d agents, %d symbols",
            run_mode, self.mode, len(agents), symbols_count,
        )

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                pass  # Windows

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            log.info("All agents cancelled — shutdown complete")

    def _request_shutdown(self) -> None:
        log.info("Shutdown signal received — cancelling agents…")
        for task in self._tasks:
            task.cancel()
