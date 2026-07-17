import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)
            logger.warning("SSE subscriber queue full, dropped")

    def subscriber_count(self) -> int:
        return len(self._subscribers)


_bus = EventBus()


def get_event_bus() -> EventBus:
    return _bus


def emit(event_type: str, data: dict[str, Any]) -> None:
    _bus.publish(event_type, data)
