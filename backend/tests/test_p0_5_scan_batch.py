from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.runtime import ScannerService
from app.store import Database


class _ScanFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.data_dir = self.base / "data"
        self.config_dir = self.base / "config"
        for d in (self.data_dir, self.config_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.database = Database(str(self.config_dir / "test.db"))
        self.database.initialize()
        self.database.update_config(
            {
                "file": {
                    "input_dir": str(self.data_dir),
                    "output_to_source_dir": False,
                    "min_size_mb": 0,
                    "max_size_mb": 8192,
                    "allowed_extensions": [".mp4"],
                    "max_pending_tasks": 1000,
                },
                "processing": {
                    "work_dir": str(self.config_dir / "work"),
                    "max_retries": 2,
                    "retry_mode": "restart",
                },
                "translation": {
                    "enabled": False,
                    "target_languages": [],
                    "max_retries": 1,
                    "api_base_url": "",
                    "api_key": "",
                    "model": "",
                    "llm_type": "openai-compatible",
                },
                "subtitle": {
                    "bilingual": False,
                    "bilingual_mode": "merge",
                    "filename_template": "{stem}.{lang}.srt",
                },
                "mux": {"enabled": False, "filename_template": "{stem}.mkv"},
            }
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def _create_video(self, name: str = "video.mp4", size: int = 256 * 1024 * 1024) -> Path:
        path = self.data_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 1024)
        stat = path.stat()
        old_mtime = stat.st_mtime
        os.utime(path, (old_mtime, old_mtime))
        return path


class ScanOnceBatchQueriesTests(_ScanFixture):
    """P0-5: scan_once must use O(1) DB execute calls instead of O(3N)."""

    def test_empty_candidate_list_produces_zero_tasks(self) -> None:
        scanner = ScannerService(self.database)
        result = scanner.scan_once()
        self.assertEqual(result.queued, 0)
        self.assertEqual(result.scanned, 0)

    def test_db_execute_calls_are_constant_not_linear(self) -> None:
        for i in range(20):
            self._create_video(f"video_{i:03d}.mp4")
        first_result = self._observe_and_get_stable_hits()
        for path in self.data_dir.iterdir():
            stat = path.stat()
            os.utime(path, (stat.st_mtime, stat.st_mtime))

        scanner = ScannerService(self.database)
        execute_calls: list[str] = []
        original_execute = Database.observe_files_batch

        def counting_observe(files):
            execute_calls.append("observe_files_batch")
            return original_execute(self.database, files)

        with patch.object(self.database, "observe_files_batch", side_effect=counting_observe):
            result = scanner.scan_once()

        self.assertEqual(execute_calls.count("observe_files_batch"), 1)

    def test_task_creation_count_matches_expected(self) -> None:
        for i in range(5):
            self._create_video(f"v{i}.mp4")
        self._observe_twice_to_get_stable_hits()

        scanner = ScannerService(self.database)
        result = scanner.scan_once()
        self.assertEqual(result.queued, 5)

    def test_files_with_active_tasks_are_skipped(self) -> None:
        for i in range(5):
            self._create_video(f"v{i}.mp4")
        observed_list = self._observe_twice_to_get_stable_hits()

        for obs in observed_list[:3]:
            self.database.create_task(
                obs["file_id"], obs["path"], obs["size_bytes"], obs["mtime"], parent_dir_ctime=0.0
            )
        scanner = ScannerService(self.database)
        result = scanner.scan_once()
        self.assertEqual(result.queued, 2)
        self.assertEqual(result.skipped, 3)

    def test_files_already_queued_are_not_re_queued_on_rescan(self) -> None:
        for i in range(3):
            self._create_video(f"v{i}.mp4")
        self._observe_twice_to_get_stable_hits()
        scanner = ScannerService(self.database)
        first = scanner.scan_once()
        self.assertEqual(first.queued, 3)
        second = scanner.scan_once()
        self.assertEqual(second.queued, 0)

    def _observe_and_get_stable_hits(self):
        paths = list(self.data_dir.glob("*.mp4"))
        files_data = []
        for p in paths:
            stat = p.stat()
            files_data.append((str(p), int(stat.st_size), float(stat.st_mtime)))
        return self.database.observe_files_batch(files_data)

    def _observe_twice_to_get_stable_hits(self):
        first = self._observe_and_get_stable_hits()
        time.sleep(0.01)
        return self._observe_and_get_stable_hits()


if __name__ == "__main__":
    unittest.main()
