"""Tests for scan_enabled guard at scan_once entry.

Bug fix: POST /api/admin/scans/run used to bypass the scan_enabled=False
setting by calling scan_once() directly, while the scanner daemon's
run_forever loop correctly checked it. This caused tasks to be created and
the worker to consume them even when the user had paused scanning in the UI.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import ScannerService
from app.store import Database


def _make_app_with_db(tmpdir: Path) -> tuple[TestClient, Database]:
    """Build a FastAPI app + initialised database rooted at *tmpdir*."""
    db = Database(str(tmpdir / "test.db"))
    db.initialize()
    db.update_config(
        {
            "file": {
                "input_dir": str(tmpdir),
                "output_to_source_dir": False,
                "min_size_mb": 0,
                "max_size_mb": 8192,
                "allowed_extensions": [".mp4"],
                "max_pending_tasks": 1000,
                "scan_enabled": True,
            },
            "processing": {
                "work_dir": str(tmpdir / "work"),
                "max_retries": 0,
                "retry_mode": "restart",
            },
            "translation": {
                "enabled": False,
                "target_languages": [],
                "api_base_url": "",
                "api_key": "",
                "model": "",
                "llm_type": "openai-compatible",
            },
            "subtitle": {"bilingual": False, "bilingual_mode": "merge", "filename_template": "{stem}.{lang}.srt"},
            "mux": {"enabled": False, "filename_template": "{stem}.mkv"},
        }
    )

    app = create_app()
    app.state.database = db
    app.state.model_manager = None

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    app.router.lifespan_context = _noop_lifespan

    return TestClient(app), db


def _make_video(tmpdir: Path, name: str, size: int = 1024) -> Path:
    p = tmpdir / name
    p.write_bytes(b"\x00" * size)
    return p


def _observe_twice(db: Database, tmpdir: Path) -> list[dict]:
    """Observe all *.mp4 files twice so stable_hits reaches 2."""
    paths = sorted(tmpdir.glob("*.mp4"))
    files_data = []
    for p in paths:
        stat = p.stat()
        files_data.append((str(p), int(stat.st_size), float(stat.st_mtime)))
    db.observe_files_batch(files_data)
    time.sleep(0.01)
    return db.observe_files_batch(files_data)


# ---------------------------------------------------------------------------
# scan_once direct calls
# ---------------------------------------------------------------------------


class ScanOnceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.client, self.db = _make_app_with_db(self.tmpdir)
        _make_video(self.tmpdir, "a.mp4")
        _make_video(self.tmpdir, "b.mp4")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scan_once_when_enabled_creates_tasks(self) -> None:
        """Sanity check: with scan_enabled=True and stable_hits=2, scan_once creates tasks."""
        self.db.update_config({"file": {"scan_enabled": True}})
        _observe_twice(self.db, self.tmpdir)
        result = ScannerService(self.db).scan_once()
        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.queued, 2)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(self.db.count_tasks_by_status("pending"), 2)

    def test_scan_once_when_disabled_creates_no_tasks(self) -> None:
        """Bug fix: with scan_enabled=False, scan_once creates NO tasks (even if files are stable)."""
        self.db.update_config({"file": {"scan_enabled": False}})
        _observe_twice(self.db, self.tmpdir)
        result = ScannerService(self.db).scan_once()
        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.queued, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(self.db.count_tasks_by_status("pending"), 0)

    def test_scan_once_default_treats_missing_field_as_enabled(self) -> None:
        """Backward compat: if scan_enabled field is missing, default to enabled."""
        cfg = self.db.get_config()
        cfg["file"].pop("scan_enabled", None)
        self.db.update_config(cfg)
        _observe_twice(self.db, self.tmpdir)
        result = ScannerService(self.db).scan_once()
        self.assertEqual(result.queued, 2)

    def test_scan_once_disabled_does_not_call_observe(self) -> None:
        """When disabled, scan_once must not touch the files table at all (guard short-circuits)."""
        from unittest.mock import patch
        self.db.update_config({"file": {"scan_enabled": False}})
        with patch.object(self.db, "observe_files_batch") as mock_observe:
            result = ScannerService(self.db).scan_once()
        mock_observe.assert_not_called()
        self.assertEqual(result.queued, 0)

    def test_scan_once_disabled_records_scan_result(self) -> None:
        """Even when disabled, scan_once records a scan result (so status endpoint still works)."""
        self.db.update_config({"file": {"scan_enabled": False}})
        ScannerService(self.db).scan_once()
        status = self.db.get_scan_status()
        self.assertEqual(status["queued"], 0)
        self.assertEqual(status["scanned"], 0)


# ---------------------------------------------------------------------------
# API endpoint POST /api/admin/scans/run
# ---------------------------------------------------------------------------


class ScanApiEndpointGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.client, self.db = _make_app_with_db(self.tmpdir)
        _make_video(self.tmpdir, "c.mp4")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_api_run_scan_when_disabled_does_not_create_tasks(self) -> None:
        """The API endpoint must also respect scan_enabled (regression for the bug)."""
        self.db.update_config({"file": {"scan_enabled": False}})
        _observe_twice(self.db, self.tmpdir)
        resp = self.client.post("/api/admin/scans/run")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["scanned"], 0)
        self.assertEqual(body["queued"], 0)
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(self.db.count_tasks_by_status("pending"), 0)

    def test_api_run_scan_when_enabled_creates_task(self) -> None:
        self.db.update_config({"file": {"scan_enabled": True}})
        _observe_twice(self.db, self.tmpdir)
        resp = self.client.post("/api/admin/scans/run")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["queued"], 1)
        self.assertEqual(self.db.count_tasks_by_status("pending"), 1)

    def test_toggle_scan_enabled_then_api_respects_new_value(self) -> None:
        """Toggle from enabled to disabled, then call API: no new tasks."""
        self.db.update_config({"file": {"scan_enabled": True}})
        _observe_twice(self.db, self.tmpdir)
        resp1 = self.client.post("/api/admin/scans/run")
        self.assertEqual(resp1.json()["queued"], 1)

        self.db.update_config({"file": {"scan_enabled": False}})
        resp2 = self.client.post("/api/admin/scans/run")
        self.assertEqual(resp2.json()["queued"], 0)
        # Existing task from first call is still pending; only the second call
        # should not have added a new one.
        self.assertEqual(self.db.count_tasks_by_status("pending"), 1)


if __name__ == "__main__":
    unittest.main()
