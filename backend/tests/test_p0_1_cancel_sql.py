from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import Database


class _DatabaseFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_dir = self.base / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.database = Database(str(self.config_dir / "test.db"))
        self.database.initialize()
        self.database.update_config(
            {
                "file": {
                    "input_dir": str(self.base / "data"),
                    "output_to_source_dir": False,
                    "min_size_mb": 0,
                    "allowed_extensions": [".mp4"],
                },
                "processing": {
                    "work_dir": str(self.config_dir / "work"),
                    "max_retries": 2,
                    "retry_mode": "restart",
                },
                "translation": {
                    "enabled": False,
                    "target_languages": ["zh-CN"],
                    "max_retries": 1,
                    "api_base_url": "https://api.openai.com",
                    "api_key": "",
                    "model": "gpt-4o-mini",
                    "llm_type": "openai-compatible",
                },
                "subtitle": {
                    "bilingual": True,
                    "bilingual_mode": "merge",
                    "filename_template": "{stem}.{lang}.srt",
                },
                "mux": {"enabled": False, "filename_template": "{stem}.mkv"},
            }
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def _create_and_claim_task(self) -> dict:
        observed = self.database.observe_file("/fake/video.mp4", 1024, 1000.0)
        task = self.database.create_task(
            observed["file_id"], "/fake/video.mp4", 1024, 1000.0, parent_dir_ctime=1000.0
        )
        return self.database.claim_next_pending_task()


class MarkTaskCancelledTests(_DatabaseFixture):
    """P0-1: mark_task_cancelled must set progress=0, error_message=<msg>, finished_at=<ts>."""

    def test_cancelled_task_gets_zero_progress(self) -> None:
        task = self._create_and_claim_task()
        self.database.mark_task_cancelled(task["id"], "run_asr", "user cancelled")
        reloaded = self.database.get_task(task["id"])
        self.assertEqual(reloaded["status"], "cancelled")
        self.assertEqual(reloaded["progress"], 0)

    def test_cancelled_task_gets_correct_error_message(self) -> None:
        task = self._create_and_claim_task()
        custom_msg = "manually stopped by operator"
        self.database.mark_task_cancelled(task["id"], "translate", custom_msg)
        reloaded = self.database.get_task(task["id"])
        self.assertEqual(reloaded["error_message"], custom_msg)

    def test_cancelled_task_gets_finished_at_timestamp(self) -> None:
        task = self._create_and_claim_task()
        self.database.mark_task_cancelled(task["id"], "run_asr")
        reloaded = self.database.get_task(task["id"])
        self.assertIsNotNone(reloaded["finished_at"])
        self.assertGreater(len(reloaded["finished_at"]), 10)

    def test_cancelled_task_stage_is_updated(self) -> None:
        task = self._create_and_claim_task()
        self.database.mark_task_cancelled(task["id"], "text_process", "halted")
        reloaded = self.database.get_task(task["id"])
        self.assertEqual(reloaded["stage"], "text_process")

    def test_cancel_then_re_cancel_is_idempotent(self) -> None:
        task = self._create_and_claim_task()
        self.database.mark_task_cancelled(task["id"], "run_asr", "first cancel")
        reloaded = self.database.get_task(task["id"])
        self.assertEqual(reloaded["status"], "cancelled")
        self.assertEqual(reloaded["error_message"], "first cancel")
        self.database.mark_task_cancelled(task["id"], "run_asr", "second cancel")
        reloaded = self.database.get_task(task["id"])
        self.assertEqual(reloaded["status"], "cancelled")
        self.assertEqual(reloaded["error_message"], "second cancel")
        self.assertEqual(reloaded["progress"], 0)

    def test_mark_task_failure_still_works_no_regression(self) -> None:
        task = self._create_and_claim_task()
        self.database.mark_task_failure(task["id"], "run_asr", "ASR engine crashed")
        reloaded = self.database.get_task(task["id"])
        self.assertIn(reloaded["status"], ("failed", "pending"))
        if reloaded["status"] == "failed":
            self.assertEqual(reloaded["error_message"], "ASR engine crashed")


if __name__ == "__main__":
    unittest.main()
