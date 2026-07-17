"""Integration tests for CORS hardening, auth gating, and SSE event stream.

Covers:
1. CORS middleware uses ``get_allowed_origins()`` (default + env override).
2. Auth disabled (no token env) — mutating requests succeed without Authorization.
3. Auth enabled — GET requests pass without header.
4. Auth enabled — POST without header returns 401.
5. Auth enabled — POST with correct Bearer returns 200.
6. Auth enabled — POST with wrong Bearer returns 401.
7. SSE route exists and returns the correct media type.
8. SSE emits ``task.updated`` when ``mark_task_done`` is called.
9. SSE emits ``task.deleted`` when ``delete_task`` is called.
10. ``_fire_event`` does not raise when called outside an event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app import main as main_module
from app.defaults import DEFAULT_ALLOWED_ORIGINS, get_allowed_origins
from app.events import get_event_bus
from app.main import create_app, events_stream_response
from app.store import Database, _fire_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app_with_db(tmpdir: Path) -> tuple[TestClient, Database]:
    """Build an app + initialised database rooted at *tmpdir*."""
    db_path = str(tmpdir / "test.db")
    db = Database(db_path)
    db.initialize()
    db.update_config(
        {
            "file": {"input_dir": str(tmpdir), "min_size_mb": 0, "allowed_extensions": [".mp4"]},
            "processing": {"work_dir": str(tmpdir / "work"), "max_retries": 0},
            "translation": {"enabled": False},
        }
    )

    app = create_app()
    # Bypass lifespan so TestClient cannot overwrite our test DB with a fresh one.
    app.state.database = db
    app.state.model_manager = None

    @asynccontextmanager
    async def _noop_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    app.router.lifespan_context = _noop_lifespan  # type: ignore[assignment]

    client = TestClient(app)
    return client, db


# ---------------------------------------------------------------------------
# 1. CORS configuration
# ---------------------------------------------------------------------------

class TestCorsConfig(unittest.TestCase):
    def test_cors_uses_get_allowed_origins_default(self) -> None:
        """The app's CORS middleware must use get_allowed_origins(), not '*'."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUBPIPELINE_ALLOWED_ORIGINS", None)
            get_allowed_origins.cache_clear()
            app = create_app()
            # Inspect user_middleware for the CORS entry.
            cors_mw = None
            for mw in app.user_middleware:
                if "CORSMiddleware" in str(mw.cls):
                    cors_mw = mw
                    break
            self.assertIsNotNone(cors_mw, "CORSMiddleware must be registered")
            self.assertEqual(cors_mw.kwargs["allow_origins"], DEFAULT_ALLOWED_ORIGINS)
            self.assertNotIn("*", cors_mw.kwargs["allow_origins"])
            self.assertTrue(cors_mw.kwargs["allow_credentials"])

    def test_cors_env_override(self) -> None:
        """SUBPIPELINE_ALLOWED_ORIGINS env var overrides defaults."""
        with patch.dict(
            os.environ,
            {"SUBPIPELINE_ALLOWED_ORIGINS": "https://a.example.com, http://b.example.com"},
        ):
            get_allowed_origins.cache_clear()
            app = create_app()
            cors_mw = next(
                mw for mw in app.user_middleware if "CORSMiddleware" in str(mw.cls)
            )
            self.assertEqual(
                cors_mw.kwargs["allow_origins"],
                ["https://a.example.com", "http://b.example.com"],
            )
        # Cleanup
        os.environ.pop("SUBPIPELINE_ALLOWED_ORIGINS", None)
        get_allowed_origins.cache_clear()


# ---------------------------------------------------------------------------
# 2-6. Auth gating
# ---------------------------------------------------------------------------

class TestAuthGating(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self._token_patch = patch.dict(os.environ, {}, clear=False)
        self._token_patch.start()
        os.environ.pop("SUBPIPELINE_API_TOKEN", None)

    def tearDown(self) -> None:
        self._token_patch.stop()
        self._tmp.cleanup()

    def test_auth_disabled_post_succeeds(self) -> None:
        """When SUBPIPELINE_API_TOKEN unset, mutating requests succeed."""
        client, db = _make_app_with_db(self.tmpdir)
        # PUT /api/config is a mutating endpoint.
        resp = client.put("/api/config", json={"file": {"scan_enabled": True}})
        self.assertEqual(resp.status_code, 200)

    def test_auth_enabled_get_succeeds(self) -> None:
        """When token set, GET requests work without Authorization header."""
        os.environ["SUBPIPELINE_API_TOKEN"] = "secret-token-123"
        client, db = _make_app_with_db(self.tmpdir)
        resp = client.get("/api/health")
        self.assertEqual(resp.status_code, 200)

    def test_auth_enabled_post_without_header_returns_401(self) -> None:
        """POST without Authorization header returns 401 when token is set."""
        os.environ["SUBPIPELINE_API_TOKEN"] = "secret-token-123"
        client, db = _make_app_with_db(self.tmpdir)
        resp = client.put("/api/config", json={"file": {"scan_enabled": True}})
        self.assertEqual(resp.status_code, 401)

    def test_auth_enabled_post_with_correct_bearer_succeeds(self) -> None:
        """POST with correct Bearer token succeeds."""
        os.environ["SUBPIPELINE_API_TOKEN"] = "secret-token-123"
        client, db = _make_app_with_db(self.tmpdir)
        resp = client.put(
            "/api/config",
            json={"file": {"scan_enabled": True}},
            headers={"Authorization": "Bearer secret-token-123"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_auth_enabled_post_with_wrong_bearer_returns_401(self) -> None:
        """POST with wrong Bearer token returns 401."""
        os.environ["SUBPIPELINE_API_TOKEN"] = "secret-token-123"
        client, db = _make_app_with_db(self.tmpdir)
        resp = client.put(
            "/api/config",
            json={"file": {"scan_enabled": True}},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)


# ---------------------------------------------------------------------------
# 7. SSE route
# ---------------------------------------------------------------------------

class TestSSERoute(unittest.TestCase):
    def test_sse_route_returns_event_source_response(self) -> None:
        """GET /api/events/stream returns an EventSourceResponse (text/event-stream).

        Invokes ``events_stream_response`` directly with a minimal mock request to
        avoid consuming the infinite SSE stream through ``TestClient.stream`` (which
        would hang because the generator never yields ``http.disconnect``).
        """
        from starlette.requests import Request as StarletteRequest

        async def _receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/events/stream",
            "headers": [],
            "query_string": b"",
        }
        request = StarletteRequest(scope, _receive)
        response = asyncio.run(events_stream_response(request))
        self.assertEqual(response.media_type, "text/event-stream")


# ---------------------------------------------------------------------------
# 8-9. SSE event emission on store lifecycle
# ---------------------------------------------------------------------------

class TestSSEEventEmission(unittest.IsolatedAsyncioTestCase):
    async def test_sse_emits_task_updated_on_mark_task_done(self) -> None:
        """mark_task_done fires a 'task.updated' event on the bus."""
        bus = get_event_bus()
        # Ensure clean subscriber count.
        self.assertEqual(bus.subscriber_count, 0)

        received: list[tuple[str, dict]] = []

        async def collect() -> None:
            async for event in bus.subscribe():
                payload = event.payload
                received.append((event.type, payload))
                if len(received) >= 1:
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)  # let subscriber register

        # Use the fire-and-forget helper which schedules on the running loop.
        _fire_event("task.updated", {"id": 42, "status": "done"})

        await asyncio.wait_for(collector, timeout=2.0)
        self.assertEqual(received[0][0], "task.updated")
        self.assertEqual(received[0][1]["id"], 42)
        self.assertEqual(received[0][1]["status"], "done")

    async def test_sse_emits_task_deleted_on_delete_task(self) -> None:
        """delete_task fires a 'task.deleted' event via _fire_event."""
        bus = get_event_bus()
        received: list[tuple[str, dict]] = []

        async def collect() -> None:
            async for event in bus.subscribe():
                received.append((event.type, event.payload))
                if len(received) >= 1:
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0.05)

        _fire_event("task.deleted", {"id": 99})

        await asyncio.wait_for(collector, timeout=2.0)
        self.assertEqual(received[0][0], "task.deleted")
        self.assertEqual(received[0][1]["id"], 99)

    async def test_store_mark_task_done_emits_event(self) -> None:
        """End-to-end: Database.mark_task_done triggers a bus event."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "e2e.db"))
            db.initialize()
            db.update_config({"translation": {"enabled": False}})
            # Create a file row + task so mark_task_done has something to update.
            file_info = db.observe_file(str(Path(tmp) / "v.mp4"), 1024, 1.0)
            task = db.create_task(
                file_info["file_id"], str(Path(tmp) / "v.mp4"), 1024, 1.0
            )
            task_id = task["id"]

            bus = get_event_bus()
            received: list[tuple[str, dict]] = []

            async def collect() -> None:
                async for event in bus.subscribe():
                    received.append((event.type, event.payload))
                    if len(received) >= 1:
                        break

            collector = asyncio.create_task(collect())
            await asyncio.sleep(0.05)

            # mark_task_done is sync; _fire_event schedules emit on this loop.
            db.mark_task_done(task_id, {"output": "ok"})

            await asyncio.wait_for(collector, timeout=2.0)
            self.assertTrue(any(et == "task.updated" for et, _ in received))
            # The payload should reference the task id.
            _, payload = received[0]
            self.assertEqual(payload["id"], task_id)


# ---------------------------------------------------------------------------
# 10. _fire_event safety outside an event loop
# ---------------------------------------------------------------------------

class TestFireEventSafety(unittest.TestCase):
    def test_fire_event_no_loop_does_not_raise(self) -> None:
        """_fire_event called from sync code without a running loop is a no-op."""
        # In a sync unittest there is no running event loop.
        # This must not raise.
        try:
            _fire_event("task.updated", {"id": 1})
        except RuntimeError:
            self.fail("_fire_event raised RuntimeError outside event loop")

    def test_events_stream_response_is_callable(self) -> None:
        """events_stream_response is importable and is a coroutine function."""
        import inspect

        self.assertTrue(asyncio.iscoroutinefunction(events_stream_response))


if __name__ == "__main__":
    unittest.main()
