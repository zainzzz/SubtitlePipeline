"""In-process event bus for Server-Sent Events (SSE).

Provides pub/sub primitives that the API layer (``app/main.py``) exposes via the
``/api/events/stream`` endpoint. Other modules (scanner, worker, model_manager,
store helpers) call :func:`emit` to broadcast state changes; each connected SSE
client receives events through its own :class:`asyncio.Queue`.

Design notes
------------
* **Not persisted** — events live only in-flight. A subscriber that connects
  after an event was published will not see it. Persisting to SQLite for replay
  is a future enhancement.
* **In-process only** — the bus is a module-level singleton shared by the API
  server process. The scanner and worker run as separate processes (see
  ``runtime.py``) and currently cannot reach this bus. Cross-process delivery
  would require a shared transport (Redis pub/sub, postgres LISTEN/NOTIFY, or
  a periodic DB poller) and is intentionally out of scope here.
* **Back-pressure aware** — each subscriber queue is bounded. When a slow
  consumer's queue is full, new events are *dropped for that consumer only* so
  a single stalled client cannot block the whole bus.
* **Race-safe** — all subscriber-set mutations go through an :class:`asyncio.Lock`,
  so concurrent subscribe/unsubscribe/publish cannot corrupt internal state.

Integration points (owned by other agents — do NOT edit here)
-------------------------------------------------------------
* ``app/main.py`` will add::

      from app.events import event_stream
      from sse_starlette.sse import EventSourceResponse

      @app.get("/api/events/stream")
      async def sse_stream():
          async def gen():
              async for event_type, data in event_stream():
                  yield {"event": event_type, "data": data}
          return EventSourceResponse(gen())

* ``app/store.py`` (or the API handlers that mutate tasks) will call::

      from app.events import emit, task_to_event_payload
      await emit("task.updated", task_to_event_payload(task_row))
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Dict, Set

__all__ = [
    "Event",
    "EventBus",
    "emit",
    "event_stream",
    "get_event_bus",
    "task_to_event_payload",
]

# Bounded queue size per subscriber. Events beyond this are dropped for the
# offending subscriber only (back-pressure) rather than blocking publishers.
_SUBSCRIBER_QUEUE_MAXSIZE = 256


@dataclass
class Event:
    """A single broadcast event.

    Attributes
    ----------
    type:
        Dotted event name. Canonical types used in this codebase:
        ``task.updated``, ``task.deleted``, ``scan.progress``,
        ``model.download_progress``.
    payload:
        Arbitrary JSON-serialisable dict with event-specific fields.
    timestamp:
        Unix timestamp (seconds since epoch, UTC) set at publish time.
    """

    type: str
    payload: Dict[str, Any]
    timestamp: float


class EventBus:
    """In-process pub/sub for SSE.

    Each subscriber gets its own bounded :class:`asyncio.Queue`. ``publish``
    fans out to all current subscribers atomically under a lock.
    """

    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue[Event]] = set()
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        """Current number of live subscriber queues (introspection / tests)."""
        return len(self._subscribers)

    async def subscribe(self) -> AsyncIterator[Event]:
        """Async generator yielding :class:`Event` objects as they arrive.

        The returned iterator runs forever; the caller is responsible for
        breaking out (typically when the SSE client disconnects). On exit the
        subscriber queue is removed from the bus under the lock, so there is
        no leak even on cancellation.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            # Generator was closed (GeneratorExit / CancelledError / break).
            # Always remove the queue so we do not leak dead subscribers.
            async with self._lock:
                self._subscribers.discard(queue)

    async def publish(self, event: Event) -> None:
        """Broadcast *event* to every current subscriber.

        Full queues (slow consumers) are skipped silently — the event is
        dropped for them but delivered to everyone else. We never block the
        publisher.
        """
        async with self._lock:
            # Snapshot under the lock so concurrent subscribe/unsubscribe
            # cannot mutate the set while we iterate.
            subscribers = list(self._subscribers)
        # put_nowait outside the lock: a full queue raises immediately and we
        # do not want to hold the lock during per-subscriber enqueue work.
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer — drop this event for them. They will keep
                # receiving subsequent events once they drain.
                continue


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_BUS: EventBus = EventBus()


def get_event_bus() -> EventBus:
    """Return the process-wide :class:`EventBus` singleton."""
    return _BUS


async def emit(event_type: str, payload: Dict[str, Any]) -> None:
    """Convenience wrapper: build an :class:`Event` and publish it."""
    await _BUS.publish(
        Event(type=event_type, payload=dict(payload), timestamp=time.time())
    )


async def event_stream() -> AsyncIterator[tuple[str, str]]:
    """SSE response helper (plain async generator).

    Yields ``(event_type, json_string)`` tuples, one per event. The JSON
    string is the full :class:`Event` serialised via :func:`dataclasses.asdict`
    so clients receive ``{type, payload, timestamp}``.

    Usage (in ``app/main.py``)::

        async for event_type, data in event_stream():
            yield {"event": event_type, "data": data}
    """
    async for event in get_event_bus().subscribe():
        yield event.type, json.dumps(asdict(event), ensure_ascii=False)


def task_to_event_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a task row (dict) into a serialisable SSE payload.

    Tolerant of missing keys — emits ``None`` rather than raising so that
    partial task dicts (e.g. freshly created rows) still produce a valid
    event. Callers that need stricter validation should check the row before
    calling this.
    """
    keys = ("id", "status", "stage", "progress", "file_path", "updated_at")
    return {key: task.get(key) for key in keys}
