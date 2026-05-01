"""BaseAgent — every agent inherits this for a resilient safe_run loop."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from .event_bus import EventBus


class BaseAgent(ABC):
    _bedrock_clients: dict[str, Any] = {}  # region → shared client

    def __init__(self, bus: EventBus, name: str):
        self.bus = bus
        self.name = name
        self.log = logging.getLogger(name)

    @classmethod
    def get_bedrock(cls, region: str = "us-east-1") -> Any:
        """Shared Bedrock client per region with an enlarged connection pool.

        All agents share this so the pool is not fragmented across instances.
        """
        if region not in cls._bedrock_clients:
            import boto3
            from botocore.config import Config  # type: ignore[import-untyped]
            cls._bedrock_clients[region] = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(max_pool_connections=50),
            )
        return cls._bedrock_clients[region]

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
