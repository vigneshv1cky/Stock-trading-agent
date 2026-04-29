"""BaseAgent — every agent inherits this for a resilient safe_run loop."""

import asyncio
import logging
from abc import ABC, abstractmethod

from .event_bus import EventBus


class BaseAgent(ABC):
    def __init__(self, bus: EventBus, name: str):
        self.bus = bus
        self.name = name
        self.log = logging.getLogger(name)

    @abstractmethod
    async def run(self) -> None: ...

    async def safe_run(self) -> None:
        """Wraps run() so a crash restarts the agent after a 5-second back-off."""
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                self.log.info("Cancelled — shutting down")
                raise
            except Exception as exc:
                self.log.exception("Crashed — restarting in 5 s: %s", exc)
                await asyncio.sleep(5)
