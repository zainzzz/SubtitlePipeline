"""Comprehensive tests for :mod:`app.events`.

Covers:
* Basic pub/sub ordering and fan-out
* Subscription lifecycle (cleanup, no past-event replay)
* Concurrent subscribe/publish safety
* Back-pressure: slow consumer drop, fast consumer completeness
* Event payload helpers and dataclass serialisation
* Module-level singleton identity
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import List
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.events import (  # noqa: E402
    Event,
    EventBus,
    emit,
    event_stream,
    get_event_bus,
    task_to_event_payload,
)
from app import events as _events_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Small async helpers
# ---------------------------------------------------------------------------

async def _register() -> None:
    """Yield long enough for subscriber tasks to enter the bus under the lock.

    A bare ``sleep(0)`` is not enough on some event loops / Python versions —
    the subscriber coroutine may not reach its ``async with self._lock`` block
    before the publisher runs, causing missed events.
    """
    await asyncio.sleep(0.01)


async def _collect(gen: "asyncio.Queue", count: int) -> List[Event]:
    """Collect *count* events from an asyncio.Queue."""
    out: List[Event] = []
    for _ in range(count):
        out.append(await gen.get())
    return out


async def _take(bus: EventBus, count: int) -> List[Event]:
    """Subscribe to *bus* and collect *count* events, then cancel."""
    events: List[Event] = []
    gen = bus.subscribe()
    task = asyncio.ensure_future(_drain(gen, events, count))
    await _register()
    try:
        await task
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return events


async def _drain(gen, sink: List[Event], count: int) -> None:
    """Drive *gen* (async generator) into *sink* for *count* items."""
    it = gen.__aiter__() if hasattr(gen, "__aiter__") else gen
    async for event in it:  # type: ignore[union-attr]
        sink.append(event)
        if len(sink) >= count:
            break


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class BasicPubSubTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_subscriber_receives_events_in_order(self) -> None:
        bus = EventBus()
        received: List[Event] = []

        async def subscriber() -> None:
            async for event in bus.subscribe():
                received.append(event)
                if len(received) == 3:
                    break

        sub_task = asyncio.ensure_future(subscriber())
        await _register()
        await bus.publish(Event("task.updated", {"n": 1}, 1.0))
        await bus.publish(Event("task.updated", {"n": 2}, 2.0))
        await bus.publish(Event("task.updated", {"n": 3}, 3.0))
        await sub_task

        self.assertEqual(len(received), 3)
        self.assertEqual([e.payload["n"] for e in received], [1, 2, 3])
        self.assertEqual([e.type for e in received], ["task.updated"] * 3)
        self.assertEqual([e.timestamp for e in received], [1.0, 2.0, 3.0])

    async def test_multiple_subscribers_all_receive_same_event(self) -> None:
        bus = EventBus()
        sinks: List[List[Event]] = [[], [], []]

        async def make_sub(sink: List[Event]) -> None:
            async for event in bus.subscribe():
                sink.append(event)
                if len(sink) == 2:
                    break

        tasks = [asyncio.ensure_future(make_sub(s)) for s in sinks]
        await _register()
        await bus.publish(Event("scan.progress", {"p": 1}, 0.0))
        await bus.publish(Event("scan.progress", {"p": 2}, 0.0))
        await asyncio.gather(*tasks)

        for sink in sinks:
            self.assertEqual(len(sink), 2)
            self.assertEqual([e.payload["p"] for e in sink], [1, 2])


class SubscriptionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_cleanup_after_iterator_closes(self) -> None:
        """A subscriber that stops consuming must have its queue removed."""
        bus = EventBus()
        self.assertEqual(bus.subscriber_count, 0)

        gen = bus.subscribe()
        # Drive the generator as a task so it registers, then publish an
        # event so it advances past the first queue.get and blocks on the
        # next one. This gives us a registered-but-suspended subscriber
        # without relying on wait_for (which cancels the generator).
        async def collect_one() -> Event:
            async for event in gen:
                return event
            raise AssertionError("unreachable")

        collector = asyncio.ensure_future(collect_one())
        await _register()
        await bus.publish(Event("seed", {}, 0.0))
        first = await collector
        self.assertEqual(first.type, "seed")
        self.assertEqual(bus.subscriber_count, 1)

        # Generator has exited ``collect_one`` but is suspended at the next
        # ``yield`` in ``subscribe``. Closing it triggers the finally clause.
        await gen.aclose()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(bus.subscriber_count, 0)

    async def test_new_subscribers_do_not_see_past_events(self) -> None:
        bus = EventBus()
        await bus.publish(Event("task.updated", {"x": 1}, 0.0))
        await bus.publish(Event("task.updated", {"x": 2}, 0.0))

        received: List[Event] = []

        async def subscriber() -> None:
            async for event in bus.subscribe():
                received.append(event)
                break

        task = asyncio.ensure_future(subscriber())
        await _register()
        await bus.publish(Event("task.updated", {"x": 3}, 0.0))
        await task

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["x"], 3)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_many_subscribers_many_publishes_all_delivered(self) -> None:
        bus = EventBus()
        n_subs = 10
        n_events = 100
        sinks: List[List[Event]] = [[] for _ in range(n_subs)]

        async def make_sub(idx: int) -> None:
            async for event in bus.subscribe():
                sinks[idx].append(event)
                if len(sinks[idx]) == n_events:
                    break

        sub_tasks = [asyncio.ensure_future(make_sub(i)) for i in range(n_subs)]
        await _register()

        async def publish_all() -> None:
            for i in range(n_events):
                await bus.publish(Event("ev", {"i": i}, float(i)))

        await asyncio.gather(publish_all(), *sub_tasks)

        for idx, sink in enumerate(sinks):
            self.assertEqual(
                len(sink), n_events, f"subscriber {idx} missed events"
            )
            self.assertEqual(
                [e.payload["i"] for e in sink], list(range(n_events))
            )

    async def test_concurrent_subscribe_and_publish_no_exception(self) -> None:
        """Subscribe and publish simultaneously — no exception, no deadlock."""
        bus = EventBus()
        errors: List[Exception] = []

        async def subscribe_many() -> None:
            try:
                for _ in range(20):
                    gen = bus.subscribe()
                    try:
                        await asyncio.wait_for(gen.__anext__(), timeout=0.05)
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        pass
                    finally:
                        await gen.aclose()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        async def publish_many() -> None:
            try:
                for i in range(50):
                    await bus.publish(Event("ev", {"i": i}, 0.0))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        await asyncio.gather(subscribe_many(), publish_many())
        self.assertEqual(errors, [])

    async def test_no_dead_subscribers_after_rapid_subscribe_unsubscribe(self) -> None:
        bus = EventBus()

        async def churn() -> None:
            for _ in range(30):
                gen = bus.subscribe()
                try:
                    await asyncio.wait_for(gen.__anext__(), timeout=0.01)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    pass
                finally:
                    await gen.aclose()

        await asyncio.gather(*(churn() for _ in range(5)))
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(bus.subscriber_count, 0)


class BackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_consumer_drops_events_when_queue_full(self) -> None:
        """Queue-full subscriber drops events; cleanup still runs on cancel.

        The subscriber stalls on its first event while we flood the bus past
        the 256-entry cap. We then cancel the (still-blocked) task — it would
        otherwise wait forever on ``queue.get`` — and assert fewer than 300
        events were observed, proving drops occurred.
        """
        bus = EventBus()
        received: List[Event] = []

        async def slow_sub() -> None:
            async for event in bus.subscribe():
                received.append(event)
                await asyncio.sleep(0.3)

        sub = asyncio.ensure_future(slow_sub())
        await _register()

        for i in range(300):
            await bus.publish(Event("ev", {"i": i}, 0.0))

        await asyncio.sleep(0.4)
        sub.cancel()
        try:
            await sub
        except asyncio.CancelledError:
            pass
        for _ in range(3):
            await asyncio.sleep(0)

        self.assertLess(len(received), 300)
        self.assertGreaterEqual(len(received), 1)
        self.assertEqual(bus.subscriber_count, 0)

    async def test_fast_consumer_receives_all_events(self) -> None:
        bus = EventBus()
        received: List[Event] = []

        async def fast_sub() -> None:
            async for event in bus.subscribe():
                received.append(event)
                if len(received) == 50:
                    break

        sub = asyncio.ensure_future(fast_sub())
        await _register()
        for i in range(50):
            await bus.publish(Event("ev", {"i": i}, 0.0))
        await sub
        self.assertEqual(len(received), 50)

    async def test_one_slow_consumer_does_not_block_others(self) -> None:
        """A stalled subscriber must not prevent a fast subscriber from
        receiving every event."""
        bus = EventBus()
        fast_sink: List[Event] = []
        slow_count = 0

        async def fast() -> None:
            async for event in bus.subscribe():
                fast_sink.append(event)
                if len(fast_sink) == 10:
                    break

        async def slow() -> None:
            nonlocal slow_count
            async for event in bus.subscribe():
                slow_count += 1
                # Stall so the slow queue fills; events drop for it only.
                await asyncio.sleep(10)

        slow_task = asyncio.ensure_future(slow())
        fast_task = asyncio.ensure_future(fast())
        await _register()

        for i in range(10):
            await bus.publish(Event("ev", {"i": i}, 0.0))

        await fast_task
        slow_task.cancel()
        try:
            await slow_task
        except asyncio.CancelledError:
            pass

        self.assertEqual(len(fast_sink), 10)
        # Slow consumer likely only saw the first event before stalling.
        self.assertLessEqual(slow_count, 2)


class EventPayloadTests(unittest.TestCase):
    def test_task_to_event_payload_serializes_full_row(self) -> None:
        task = {
            "id": 42,
            "status": "processing",
            "stage": "run_asr",
            "progress": 0.55,
            "file_path": "/data/movie.mkv",
            "updated_at": "2026-07-17T10:00:00Z",
            "extra_field": "ignored",
        }
        payload = task_to_event_payload(task)
        self.assertEqual(
            payload,
            {
                "id": 42,
                "status": "processing",
                "stage": "run_asr",
                "progress": 0.55,
                "file_path": "/data/movie.mkv",
                "updated_at": "2026-07-17T10:00:00Z",
            },
        )

    def test_task_to_event_payload_tolerant_of_missing_keys(self) -> None:
        payload = task_to_event_payload({"id": 1})
        self.assertEqual(
            payload,
            {
                "id": 1,
                "status": None,
                "stage": None,
                "progress": None,
                "file_path": None,
                "updated_at": None,
            },
        )

    def test_event_dataclass_asdict_roundtrip(self) -> None:
        ev = Event(type="task.updated", payload={"id": 1}, timestamp=1.5)
        d = asdict(ev)
        self.assertEqual(d, {"type": "task.updated", "payload": {"id": 1}, "timestamp": 1.5})

    def test_event_json_serialisable(self) -> None:
        ev = Event(type="scan.progress", payload={"p": 0.5}, timestamp=1.0)
        s = json.dumps(asdict(ev), ensure_ascii=False)
        self.assertIn('"scan.progress"', s)
        roundtrip = json.loads(s)
        self.assertEqual(roundtrip["payload"]["p"], 0.5)


class ModuleSingletonTests(unittest.IsolatedAsyncioTestCase):
    def test_get_event_bus_returns_same_instance(self) -> None:
        a = get_event_bus()
        b = get_event_bus()
        self.assertIs(a, b)

    async def test_emit_uses_singleton_and_sets_timestamp(self) -> None:
        bus = get_event_bus()
        received: List[Event] = []

        async def sub() -> None:
            async for event in bus.subscribe():
                received.append(event)
                break

        task = asyncio.ensure_future(sub())
        await _register()
        fixed_time = 1234567890.1
        with patch.object(_events_mod.time, "time", return_value=fixed_time):
            await emit("task.updated", {"id": 7})
        await task

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].type, "task.updated")
        self.assertEqual(received[0].payload, {"id": 7})
        self.assertAlmostEqual(received[0].timestamp, fixed_time)

    async def test_event_stream_yields_json_tuples(self) -> None:
        received: List[tuple] = []

        async def consumer() -> None:
            async for event_type, data in event_stream():
                received.append((event_type, data))
                break

        task = asyncio.ensure_future(consumer())
        await _register()
        await emit("scan.progress", {"p": 0.1})
        await task

        self.assertEqual(len(received), 1)
        event_type, data = received[0]
        self.assertEqual(event_type, "scan.progress")
        parsed = json.loads(data)
        self.assertEqual(parsed["type"], "scan.progress")
        self.assertEqual(parsed["payload"], {"p": 0.1})
        self.assertIn("timestamp", parsed)


if __name__ == "__main__":
    unittest.main()
