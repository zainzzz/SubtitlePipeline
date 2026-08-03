"""Tests for the batched scanner / store operations.

Covers:
- `bulk_observe_files` upserts new files and increments stable_hits for repeats
- `bulk_observe_files` returns a dict keyed by path_key
- `bulk_check_active_tasks` returns the set of path_keys with pending/processing tasks
- `bulk_check_existing_versions` returns the set of (key, size, mtime) already in tasks
- `bulk_create_tasks` inserts all items in a single transaction and returns task dicts
- `bulk_*` methods chunk inputs > 200/1000 to avoid SQLite variable limits
- `ScannerService.scan_once` produces the same end-state (queued/skipped) as the
  old per-file implementation, while only hitting the DB a handful of times
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime import ScannerService
from app.store import Database, normalize_path


def _make_db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(str(Path(tmp.name) / "test.db"), persistent=True)
    db.initialize()
    media_dir = str(Path(tmp.name) / "media")
    db.update_config({
        "file": {
            "input_dir": media_dir,
            "min_size_mb": 0,
            "max_size_mb": 1024,
            "max_pending_tasks": 100,
            "allowed_extensions": [".mkv", ".mp4"],
        },
        "processing": {
            "work_dir": str(Path(tmp.name) / "work"),
        },
    })
    # tmp is captured by closure but Database only needs db_path — leaking the
    # TemporaryDirectory is fine for test process lifetime.
    return db


def _override(db: Database, **file_overrides) -> None:
    cfg = db.get_config()
    cfg["file"].update(file_overrides)
    db.update_config(cfg)


def _make_video(path: Path, size: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


class BulkObserveFilesTests(unittest.TestCase):
    def test_new_files_inserted_with_stable_hits_1(self) -> None:
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        b = tmp / "b.mkv"
        _make_video(a)
        _make_video(b)
        result = db.bulk_observe_files([
            {"file_path": str(a), "size_bytes": 1024, "mtime": 100.0},
            {"file_path": str(b), "size_bytes": 1024, "mtime": 100.0},
        ])
        self.assertEqual(len(result), 2)
        for k, v in result.items():
            self.assertEqual(v["stable_hits"], 1)
            self.assertEqual(v["size_bytes"], 1024)

    def test_repeat_increments_stable_hits(self) -> None:
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        _make_video(a)
        # First observe
        db.bulk_observe_files([{"file_path": str(a), "size_bytes": 1024, "mtime": 100.0}])
        # Second observe with same size/mtime — should bump stable_hits
        result = db.bulk_observe_files([{"file_path": str(a), "size_bytes": 1024, "mtime": 100.0}])
        self.assertEqual(result[next(iter(result))]["stable_hits"], 2)

    def test_size_change_resets_stable_hits(self) -> None:
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        _make_video(a)
        db.bulk_observe_files([{"file_path": str(a), "size_bytes": 1024, "mtime": 100.0}])
        db.bulk_observe_files([{"file_path": str(a), "size_bytes": 1024, "mtime": 100.0}])
        # Now size changes — file was overwritten
        result = db.bulk_observe_files([{"file_path": str(a), "size_bytes": 2048, "mtime": 200.0}])
        self.assertEqual(result[next(iter(result))]["stable_hits"], 1)

    def test_empty_input_returns_empty_dict(self) -> None:
        db = _make_db()
        self.assertEqual(db.bulk_observe_files([]), {})


class BulkCheckActiveTasksTests(unittest.TestCase):
    def test_returns_path_keys_with_pending_tasks(self) -> None:
        db = _make_db()
        # Manually create a file and a pending task for it
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        _make_video(a)
        result = db.bulk_observe_files([{"file_path": str(a), "size_bytes": 1024, "mtime": 100.0}])
        file_id = result[next(iter(result))]["file_id"]
        # Manually set stable_hits high enough
        db.bulk_create_tasks([{
            "file_id": file_id, "file_path": str(a),
            "size_bytes": 1024, "mtime": 100.0,
        }])
        # Now there should be 1 active task for this path_key
        active = db.bulk_check_active_tasks(["path_doesnt_exist", "nonexistent"])
        # 'nonexistent' is just a string; the DB stores path_key (normalized).
        # We expect the existing path to be present, the others not.
        existing_path_key = normalize_path(str(a))
        active = db.bulk_check_active_tasks([existing_path_key, "stranger"])
        self.assertIn(existing_path_key, active)
        self.assertNotIn("stranger", active)

    def test_empty_input_returns_empty(self) -> None:
        db = _make_db()
        self.assertEqual(db.bulk_check_active_tasks([]), set())


class BulkCheckExistingVersionsTests(unittest.TestCase):
    def test_returns_matching_version_tuples(self) -> None:
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        _make_video(a)
        result = db.bulk_observe_files([{"file_path": str(a), "size_bytes": 1024, "mtime": 100.0}])
        file_id = result[next(iter(result))]["file_id"]
        db.bulk_create_tasks([{
            "file_id": file_id, "file_path": str(a),
            "size_bytes": 1024, "mtime": 100.0,
        }])
        # The (key, 1024, 100.0) tuple is now "existing"
        existing_path_key = normalize_path(str(a))
        existing = db.bulk_check_existing_versions([
            (existing_path_key, 1024, 100.0),
            (existing_path_key, 2048, 200.0),  # different size → not existing
            ("/no/such/path", 1, 1.0),
        ])
        self.assertIn((existing_path_key, 1024, 100.0), existing)
        self.assertNotIn((existing_path_key, 2048, 200.0), existing)
        self.assertNotIn(("/no/such/path", 1, 1.0), existing)

    def test_empty_input_returns_empty(self) -> None:
        db = _make_db()
        self.assertEqual(db.bulk_check_existing_versions([]), set())


class BulkCreateTasksTests(unittest.TestCase):
    def test_inserts_all_items_returns_task_dicts(self) -> None:
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        b = tmp / "b.mkv"
        _make_video(a)
        _make_video(b)
        result = db.bulk_observe_files([
            {"file_path": str(a), "size_bytes": 1024, "mtime": 100.0},
            {"file_path": str(b), "size_bytes": 1024, "mtime": 100.0},
        ])
        a_id = result[normalize_path(str(a))]["file_id"]
        b_id = result[normalize_path(str(b))]["file_id"]
        tasks = db.bulk_create_tasks([
            {"file_id": a_id, "file_path": str(a), "size_bytes": 1024, "mtime": 100.0, "parent_dir_ctime": 0.0},
            {"file_id": b_id, "file_path": str(b), "size_bytes": 1024, "mtime": 100.0, "parent_dir_ctime": 0.0},
        ])
        self.assertEqual(len(tasks), 2)
        for t in tasks:
            self.assertIn("id", t)
            self.assertEqual(t["status"], "pending")
            self.assertEqual(t["stage"], "queued")
            self.assertGreater(t["id"], 0)

    def test_empty_input_returns_empty(self) -> None:
        db = _make_db()
        self.assertEqual(db.bulk_create_tasks([]), [])

    def test_chunks_large_input(self) -> None:
        """A batch of 500 should be processed across multiple chunks. We
        only check end-state correctness; the chunking is an internal detail."""
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        items = []
        for i in range(500):
            p = tmp / f"f{i}.mkv"
            _make_video(p, size=64)
            res = db.bulk_observe_files([{"file_path": str(p), "size_bytes": 64, "mtime": float(i)}])
            items.append({
                "file_id": res[normalize_path(str(p))]["file_id"],
                "file_path": str(p),
                "size_bytes": 64,
                "mtime": float(i),
                "parent_dir_ctime": 0.0,
            })
        tasks = db.bulk_create_tasks(items)
        self.assertEqual(len(tasks), 500)
        ids = [t["id"] for t in tasks]
        self.assertEqual(len(set(ids)), 500)  # all unique


class ScanOnceBatchBehaviorTests(unittest.TestCase):
    """End-to-end: verify the refactored scan_once produces the same queued
    / skipped outcome as the per-file implementation, but with a small
    number of DB roundtrips."""

    def test_first_scan_defers_until_stable(self) -> None:
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        a = tmp / "a.mkv"
        b = tmp / "b.mp4"
        _make_video(a)
        _make_video(b)
        scanner = ScannerService(db)
        # First scan: 2 files observed, neither is stable yet, so 0 queued
        r1 = scanner.scan_once()
        self.assertEqual(r1.queued, 0)
        self.assertEqual(r1.scanned, 2)
        # Second scan: stable_hits=2 → both should queue
        r2 = scanner.scan_once()
        self.assertEqual(r2.queued, 2)
        self.assertEqual(r2.skipped, 0)
        # Third scan: already-handled versions are skipped
        r3 = scanner.scan_once()
        self.assertEqual(r3.queued, 0)
        self.assertEqual(r3.skipped, 2)

    def test_throttle_limits_queued(self) -> None:
        db = _make_db()
        _override(db, max_pending_tasks=2)
        tmp = Path(db.get_config()["file"]["input_dir"])
        for i in range(5):
            _make_video(tmp / f"f{i}.mkv", size=64)
        scanner = ScannerService(db)
        # Prime stable_hits
        scanner.scan_once()
        r = scanner.scan_once()
        self.assertEqual(r.queued, 2)
        self.assertTrue(r.throttled)

    def test_size_filter_skips(self) -> None:
        db = _make_db()
        _override(db, min_size_mb=1)  # 1 MB
        tmp = Path(db.get_config()["file"]["input_dir"])
        _make_video(tmp / "small.mkv", size=1024)  # 1KB
        _make_video(tmp / "big.mkv", size=2 * 1024 * 1024)
        scanner = ScannerService(db)
        scanner.scan_once()
        r = scanner.scan_once()
        self.assertEqual(r.queued, 1)

    def test_db_query_count_is_small(self) -> None:
        """End-to-end perf sanity: scanning 20 files should NOT issue 60+ SQLs.
        We allow some headroom for migrations, pragmas, internal config lookups."""
        db = _make_db()
        tmp = Path(db.get_config()["file"]["input_dir"])
        for i in range(20):
            _make_video(tmp / f"f{i}.mkv", size=64)
        scanner = ScannerService(db)
        # First scan to populate stable_hits
        scanner.scan_once()
        # Patch connect to count invocations
        with patch.object(db, "connect", wraps=db.connect) as conn_wrapper:
            r = scanner.scan_once()
        # connect() is called once per transaction. We expect: 1 observe,
        # 1 version check, 1 active check, 1 create = roughly 4 connects.
        # Plus the scan_result record + config read = small overhead.
        # Use a generous bound to allow for non-batch config lookups.
        self.assertLess(conn_wrapper.call_count, 15, f"too many connect() calls: {conn_wrapper.call_count}")


if __name__ == "__main__":
    unittest.main()
