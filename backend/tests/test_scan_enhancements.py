from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.defaults import copy_default_config  # noqa: E402
from app.runtime import ScannerService, _should_skip_scan_path  # noqa: E402
from app.store import Database  # noqa: E402


class ExcludeDirsTests(unittest.TestCase):
    """黑名单:exclude_dirs + fnmatch 通配。"""

    def test_exact_dir_name_matches(self) -> None:
        self.assertTrue(_should_skip_scan_path(Path("/data/Movie/Sample/x.mkv"), {"file": {"exclude_dirs": ["Sample"]}}))

    def test_fnmatch_wildcard_matches(self) -> None:
        self.assertTrue(_should_skip_scan_path(Path("/data/预告片/x.mkv"), {"file": {"exclude_dirs": ["预告*"]}}))

    def test_no_match_passes_through(self) -> None:
        self.assertFalse(_should_skip_scan_path(Path("/data/Movie/Feature/x.mkv"), {"file": {"exclude_dirs": ["Sample"]}}))

    def test_empty_exclude_dirs_does_not_skip(self) -> None:
        self.assertFalse(_should_skip_scan_path(Path("/data/Movie/x.mkv"), {"file": {"exclude_dirs": []}}))

    def test_subpath_default_still_skipped(self) -> None:
        # .subpipeline 是内置排除,不受 exclude_dirs 影响
        self.assertTrue(_should_skip_scan_path(Path("/data/.subpipeline/x"), {"file": {"exclude_dirs": []}}))


class ScanStatusStoreTests(unittest.TestCase):
    """扫描状态 record/get roundtrip。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_initial_status_has_no_last_scan(self) -> None:
        self.assertEqual(self.db.get_scan_status(), {"last_scan_at": None})

    def test_record_then_get_roundtrip(self) -> None:
        self.db.record_scan_result({"scanned": 5, "queued": 2, "skipped": 3, "pending_count": 1, "throttled": False})
        status = self.db.get_scan_status()
        self.assertEqual(status["scanned"], 5)
        self.assertEqual(status["queued"], 2)
        self.assertEqual(status["skipped"], 3)
        self.assertFalse(status["throttled"])
        self.assertIsNotNone(status["last_scan_at"])

    def test_record_throttled_flag(self) -> None:
        self.db.record_scan_result({"scanned": 0, "queued": 0, "skipped": 0, "pending_count": 100, "throttled": True})
        self.assertTrue(self.db.get_scan_status()["throttled"])

    def test_record_overwrites_previous(self) -> None:
        self.db.record_scan_result({"scanned": 1, "queued": 0, "skipped": 0, "pending_count": 0, "throttled": False})
        self.db.record_scan_result({"scanned": 9, "queued": 0, "skipped": 0, "pending_count": 0, "throttled": False})
        self.assertEqual(self.db.get_scan_status()["scanned"], 9)


class BackpressureTests(unittest.TestCase):
    """backpressure: pending 堆积达阈值时 scan_once early-return throttled。"""

    def test_pending_at_max_returns_throttled(self) -> None:
        db = MagicMock()
        config = copy_default_config()
        config["file"]["max_pending_tasks"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            config["file"]["input_dir"] = tmp  # 避免 mkdir 默认 /data 的副作用
            db.get_config.return_value = config
            db.count_tasks_by_status.return_value = 100
            service = ScannerService(db)
            result = service.scan_once()
        self.assertTrue(result.throttled)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.queued, 0)
        self.assertEqual(result.pending_count, 100)
        db.record_scan_result.assert_called_once()


if __name__ == "__main__":
    unittest.main()
