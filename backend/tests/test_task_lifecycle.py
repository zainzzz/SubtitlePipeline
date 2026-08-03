"""Tests for the task lifecycle state machine.

Covers the 5 core transitions in `app/store.py`:
- `claim_next_pending_task` — atomically transition pending → processing
- `mark_task_done` — transition to done, persist result_payload
- `mark_task_cancelled` — transition to cancelled
- `mark_task_failure` — transition to failed (or back to pending with retry)
- `recover_orphaned_tasks` — at startup, reset processing tasks to failed

The state diagram under test:
    pending  ─claim→  processing  ─done→   done
    pending  ─claim→  processing  ─cancel→ cancelled
    pending  ─claim→  processing  ─fail→  failed  (or pending with retry)
    processing  ─recover_orphaned→  failed  (system crash recovery)

The tests use a real on-disk SQLite database (per-test tempdir) so SQL
behaviour and transactions are exercised end-to-end. The pipeline integration
is mocked by creating tasks directly via the bulk_create_tasks method.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import Database, normalize_path


def _make_db(**file_overrides) -> tuple[Database, "tempfile.TemporaryDirectory"]:
    """Spin up a fresh DB with minimal config; returns (db, tmpdir) so the
    caller can keep the tempdir alive for the duration of the test."""
    tmp = tempfile.TemporaryDirectory()
    db = Database(str(Path(tmp.name) / "test.db"), persistent=True)
    db.initialize()
    file_cfg = {
        "input_dir": str(Path(tmp.name) / "media"),
        "min_size_mb": 0,
        "max_size_mb": 1024,
        "max_pending_tasks": 100,
        "allowed_extensions": [".mkv", ".mp4"],
    }
    file_cfg.update(file_overrides)
    db.update_config({
        "file": file_cfg,
        "processing": {
            "work_dir": str(Path(tmp.name) / "work"),
            "max_retries": 1,
        },
    })
    return db, tmp


def _seed_file(db: Database, name: str = "a.mkv", size: int = 1024, mtime: float = 100.0) -> dict:
    """Insert one file row + one pending task; return the task row."""
    tmp_dir = Path(db.get_config()["file"]["input_dir"])
    p = tmp_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * size)
    observe = db.bulk_observe_files([{"file_path": str(p), "size_bytes": size, "mtime": mtime}])
    file_id = observe[normalize_path(str(p))]["file_id"]
    tasks = db.bulk_create_tasks([{
        "file_id": file_id,
        "file_path": str(p),
        "size_bytes": size,
        "mtime": mtime,
        "parent_dir_ctime": 0.0,
    }])
    return db.get_task(tasks[0]["id"])


class ClaimNextPendingTaskTests(unittest.TestCase):
    def test_returns_none_when_no_pending(self) -> None:
        db, _tmp = _make_db()
        self.assertIsNone(db.claim_next_pending_task())

    def test_claim_transitions_pending_to_processing(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        claimed = db.claim_next_pending_task()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], task["id"])
        self.assertEqual(claimed["status"], "processing")
        # Stage advances from "queued" to "extract_audio"
        self.assertEqual(claimed["stage"], "extract_audio")
        self.assertEqual(claimed["progress"], 0)

    def test_second_claim_returns_none(self) -> None:
        db, _tmp = _make_db()
        _seed_file(db)
        first = db.claim_next_pending_task()
        self.assertIsNotNone(first)
        # Only one task; second claim sees no pending
        self.assertIsNone(db.claim_next_pending_task())

    def test_two_pending_only_one_claimed(self) -> None:
        db, _tmp = _make_db()
        _seed_file(db, "a.mkv")
        _seed_file(db, "b.mkv")
        first = db.claim_next_pending_task()
        self.assertIsNotNone(first)
        # Second claim returns a different task (or None) — never the same one
        second = db.claim_next_pending_task()
        if second is not None:
            self.assertNotEqual(first["id"], second["id"])


class MarkTaskDoneTests(unittest.TestCase):
    def test_marks_done_with_result_payload(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        result = {"subtitle_paths": ["/data/a.forced.zh.srt"], "mux_path": None}
        db.mark_task_done(task["id"], result)
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["progress"], 100)
        self.assertIsNone(updated["error_message"])
        # result_payload is JSON-decoded by get_task, so it's already a dict
        self.assertEqual(updated["result_payload"], result)

    def test_final_stage_is_mux_when_mux_enabled(self) -> None:
        db, _tmp = _make_db()
        # Enable mux in config
        cfg = db.get_config()
        cfg["mux"]["enabled"] = True
        db.update_config(cfg)
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_done(task["id"], {"subtitle_paths": []})
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["stage"], "mux")

    def test_final_stage_is_output_finalize_when_mux_disabled(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_done(task["id"], {"subtitle_paths": []})
        updated = db.get_task(task["id"])
        self.assertEqual(updated["stage"], "output_finalize")

    def test_done_clears_error_message(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_failure(task["id"], "translate", "transient error")
        # Task is back in pending because max_retries > retry_count
        db.claim_next_pending_task()
        db.mark_task_done(task["id"], {"subtitle_paths": []})
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "done")
        self.assertIsNone(updated["error_message"])


class MarkTaskCancelledTests(unittest.TestCase):
    def test_marks_cancelled_from_processing(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_cancelled(task["id"], "translate", "user requested cancel")
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "cancelled")
        self.assertEqual(updated["stage"], "translate")
        self.assertEqual(updated["error_message"], "user requested cancel")
        self.assertIsNotNone(updated["finished_at"])

    def test_cancelled_task_not_re_claimable(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_cancelled(task["id"], "translate", "user")
        # After cancellation the task should NOT be re-claimed
        self.assertIsNone(db.claim_next_pending_task())


class MarkTaskFailureTests(unittest.TestCase):
    def test_fails_when_max_retries_exhausted(self) -> None:
        db, _tmp = _make_db()  # default max_retries=1
        task = _seed_file(db)
        db.claim_next_pending_task()
        # First failure: should_retry=True (retry_count 0 < 1)
        result = db.mark_task_failure(task["id"], "translate", "boom")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["retry_count"], 1)
        # Second failure: should_retry=False
        db.claim_next_pending_task()
        result2 = db.mark_task_failure(task["id"], "translate", "boom again")
        self.assertEqual(result2["status"], "failed")
        self.assertEqual(result2["error_message"], "boom again")

    def test_failure_then_done_works(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        result = db.mark_task_failure(task["id"], "translate", "transient")
        self.assertEqual(result["status"], "pending")
        # Worker can claim again and finish
        db.claim_next_pending_task()
        db.mark_task_done(task["id"], {"subtitle_paths": []})
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "done")
        self.assertEqual(updated["retry_count"], 1)

    def test_zero_max_retries_fails_immediately(self) -> None:
        db, _tmp = _make_db()
        # Override max_retries to 0
        cfg = db.get_config()
        cfg["processing"]["max_retries"] = 0
        db.update_config(cfg)
        task = _seed_file(db)
        db.claim_next_pending_task()
        result = db.mark_task_failure(task["id"], "translate", "boom")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_message"], "boom")

    def test_failure_raises_for_missing_task(self) -> None:
        db, _tmp = _make_db()
        with self.assertRaises(RuntimeError):
            db.mark_task_failure(99999, "translate", "no such task")


class RecoverOrphanedTasksTests(unittest.TestCase):
    def test_resets_processing_to_failed(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        # Stuck in processing
        db.claim_next_pending_task()
        # Simulate system crash recovery
        count = db.recover_orphaned_tasks()
        self.assertEqual(count, 1)
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "failed")
        self.assertIn("系统重启", updated["error_message"] or "")

    def test_does_not_touch_pending(self) -> None:
        db, _tmp = _make_db()
        _seed_file(db)  # pending
        count = db.recover_orphaned_tasks()
        # No processing tasks, but the pending task is untouched
        self.assertEqual(count, 0)
        # Task is still pending
        listed = db.list_tasks(page=1, page_size=10, status="pending")
        self.assertEqual(listed.total, 1)

    def test_does_not_touch_done(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_done(task["id"], {"subtitle_paths": []})
        count = db.recover_orphaned_tasks()
        self.assertEqual(count, 0)
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "done")

    def test_does_not_touch_already_failed(self) -> None:
        db, _tmp = _make_db()
        # max_retries=0 so first failure is terminal
        cfg = db.get_config()
        cfg["processing"]["max_retries"] = 0
        db.update_config(cfg)
        task = _seed_file(db)
        db.claim_next_pending_task()
        db.mark_task_failure(task["id"], "translate", "boom")
        count = db.recover_orphaned_tasks()
        self.assertEqual(count, 0)
        updated = db.get_task(task["id"])
        self.assertEqual(updated["status"], "failed")
        self.assertEqual(updated["error_message"], "boom")  # not overwritten

    def test_recover_mixed_states(self) -> None:
        """Three tasks: one pending, one stuck processing, one done.
        Only the processing one is reset."""
        db, _tmp = _make_db()
        pending_task = _seed_file(db, "a.mkv")
        stuck_task = _seed_file(db, "b.mkv")
        done_task = _seed_file(db, "c.mkv")
        # Don't go through claim_next_pending_task (it would change one of
        # the pending tasks into processing). Drive state directly via SQL
        # so each task is in a known starting state.
        with db.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='processing', stage='run_asr' WHERE id=?",
                (stuck_task["id"],),
            )
            conn.execute(
                "UPDATE tasks SET status='done', progress=100, finished_at=?, result_payload='{}' WHERE id=?",
                ("2026-01-01 00:00:00", done_task["id"]),
            )
        # Now: pending, processing, done
        count = db.recover_orphaned_tasks()
        self.assertEqual(count, 1)
        self.assertEqual(db.get_task(pending_task["id"])["status"], "pending")
        self.assertEqual(db.get_task(stuck_task["id"])["status"], "failed")
        self.assertEqual(db.get_task(done_task["id"])["status"], "done")


class FullLifecycleTests(unittest.TestCase):
    def test_full_happy_path(self) -> None:
        db, _tmp = _make_db()
        task = _seed_file(db)
        # 1. claim
        claimed = db.claim_next_pending_task()
        self.assertEqual(claimed["status"], "processing")
        # 2. progress through stages
        db.update_task_stage(task["id"], "run_asr", 30)
        db.update_task_stage(task["id"], "translate", 60)
        db.update_task_stage(task["id"], "subtitle_render", 95)
        # 3. done
        db.mark_task_done(task["id"], {"subtitle_paths": ["/data/a.srt"]})
        final = db.get_task(task["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["progress"], 100)

    def test_fail_retry_succeed(self) -> None:
        db, _tmp = _make_db()  # max_retries=1
        task = _seed_file(db)
        # First attempt: claim → fail → back to pending
        db.claim_next_pending_task()
        db.mark_task_failure(task["id"], "translate", "rate limited")
        # Worker picks it up again
        reclaimed = db.claim_next_pending_task()
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed["retry_count"], 1)
        # Second attempt succeeds
        db.mark_task_done(task["id"], {"subtitle_paths": []})
        final = db.get_task(task["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["retry_count"], 1)

    def test_fail_retry_fail_terminal(self) -> None:
        db, _tmp = _make_db()  # max_retries=1
        task = _seed_file(db)
        # First fail: should_retry=True (0 < 1), retry_count → 1, status=pending
        db.claim_next_pending_task()
        first = db.mark_task_failure(task["id"], "translate", "boom")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["retry_count"], 1)
        # Second fail: should_retry=False (1 < 1 is false), retry_count stays at 1, status=failed
        db.claim_next_pending_task()
        result = db.mark_task_failure(task["id"], "translate", "boom again")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retry_count"], 1)
        # No more pending
        self.assertIsNone(db.claim_next_pending_task())


if __name__ == "__main__":
    unittest.main()
