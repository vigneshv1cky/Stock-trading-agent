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
    NewsAgent           → symbol.analysed      (Polygon + Haiku sentiment)
    PredictorAgent      → symbol.predicted     (Haiku score)
    CriticAgent         → symbol.reviewed      (adversarial Haiku)
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
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.bus = EventBus()
        self.memory = AgentMemory()
        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    async def run(self) -> None:
        stock_symbols = list(dict.fromkeys(SCREEN_UNIVERSE))

        from .base import BaseAgent
        agents: list[BaseAgent] = [
            MacroAgent(self.bus),
            WatcherAgent(self.bus, stock_symbols),
            ScannerAgent(self.bus, stock_symbols),
            ScreenerAgent(self.bus),
            ResearchAgent(self.bus),
            NewsAgent(self.bus),
            PredictorAgent(self.bus, self.memory),
            CriticAgent(self.bus, self.memory),
            PortfolioAgent(self.bus),
            RiskAgent(self.bus),
            ExecutorAgent(self.bus, dry_run=self.dry_run),
            LearningAgent(self.bus, self.memory),
            MonitorAgent(self.bus),
        ]

        self._tasks = [
            asyncio.create_task(agent.safe_run(), name=agent.name)
            for agent in agents
        ]

        run_mode = "DRY-RUN" if self.dry_run else "LIVE"
        log.info(
            "Multi-agent trading system started [%s] — %d agents, %d symbols",
            run_mode, len(agents), len(stock_symbols),
        )

        loop = asyncio.get_running_loop()
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
