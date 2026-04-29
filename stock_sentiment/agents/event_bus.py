"""Async pub/sub event bus — in-process asyncio.Queue fan-out, no external broker needed."""

import asyncio
import time
from collections import defaultdict
from typing import Any


class EventBus:
    def __init__(self):
        self._queues: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, *topics: str) -> asyncio.Queue:
        """Return a single queue that receives messages from all requested topics."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        for topic in topics:
            self._queues[topic].append(q)
        return q

    async def publish(self, topic: str, data: Any) -> None:
        msg = {"topic": topic, "data": data, "ts": time.time()}
        for q in self._queues.get(topic, []):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow consumer — drop rather than block the publisher

    def publish_nowait(self, topic: str, data: Any) -> None:
        """Sync-safe fire-and-forget for use inside non-async callbacks."""
        msg = {"topic": topic, "data": data, "ts": time.time()}
        for q in self._queues.get(topic, []):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass
