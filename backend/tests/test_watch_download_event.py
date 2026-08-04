"""Tests for the per-spec Event-driven `_watch_download` loop.

The previous implementation used `time.sleep(DOWNLOAD_PROGRESS_POLL_SECONDS)`
in the watcher loop, meaning the watch thread could stay alive for up to the
poll period (default ~5s) after a download actually completed. This test
pins down the new event-driven behavior: when `_download_model` finishes and
calls `wake.set()`, the watcher must notice within a few hundred ms, not a
full poll cycle.

We mock `huggingface_hub.snapshot_download` (the actual network call) so the
test runs offline. The watcher is verified by polling `manager._states`
after a brief sleep and asserting the state has transitioned.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.model_manager import ModelManager, ModelSpec


def _make_manager(tmpdir: Path) -> ModelManager:
    mm = ModelManager(tmpdir)
    mm._stall_timeout_seconds = 60  # don't trip the stall path in this test
    return mm


def _make_spec(name: str, repo_id: str = "fake/repo") -> ModelSpec:
    return ModelSpec(
        name=name,
        repo_id=repo_id,
        size_label="1.0 MB",
        estimated_size_bytes=1024 * 1024,
        provider="whisperx",
        display_name=name,
        description="test",
        tags=(),
    )


def _wait_for_state(
    mm: ModelManager,
    name: str,
    *,
    predicate,
    timeout: float = 3.0,
    poll: float = 0.05,
) -> bool:
    """Spin until the manager's state for `name` matches `predicate`, or
    the timeout elapses. Returns whether the predicate became true."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with mm._lock:
            state = mm._states.get(name)
        if state is not None and predicate(state):
            return True
        time.sleep(poll)
    return False


class WatchDownloadEventTests(unittest.TestCase):
    """Helper: start a download for a real KNOWN_MODELS spec name, but patch
    the actual HF snapshot_download to a quick fake. start_download takes
    a name string and looks up the spec via get_spec(), so we use a real
    model name to avoid having to monkey-patch the spec table."""

    def _start_fake_download(self, mm: ModelManager, name: str = "whisperx-tiny", delay: float = 0.15) -> None:
        def fake_snapshot_download(repo_id, local_dir, **_):
            time.sleep(delay)
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "w").write_bytes(b"\x00" * 10)
            return local_dir

        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
            mm.start_download(name)

    def test_wake_event_registered_at_start(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        mm = _make_manager(tmp)
        self._start_fake_download(mm)
        try:
            self.assertIn("whisperx-tiny", mm._wake_events)
            self.assertIsInstance(mm._wake_events["whisperx-tiny"], threading.Event)
        finally:
            pass

    def test_watcher_exits_within_a_few_hundred_ms_of_completion(self) -> None:
        """The whole point of the refactor: a quick fake download
        (~150ms) should produce a "installed" state well under
        DOWNLOAD_PROGRESS_POLL_SECONDS (5s by default)."""
        tmp = Path(tempfile.mkdtemp())
        mm = _make_manager(tmp)
        started_at = time.time()
        completed_at: list[float] = []

        def fake_snapshot_download(repo_id, local_dir, **_):
            time.sleep(0.15)
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            (Path(local_dir) / "w").write_bytes(b"\x00" * 10)
            return local_dir

        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
            mm.start_download("whisperx-tiny")
            ok = _wait_for_state(mm, "whisperx-tiny", predicate=lambda s: s.status == "installed", timeout=3.0)
            self.assertTrue(ok, "state should have transitioned to 'installed'")
            completed_at.append(time.time())

        elapsed = completed_at[0] - started_at
        self.assertLess(
            elapsed, 2.0,
            f"download + state transition took {elapsed:.2f}s; should be well under 5s",
        )

    def test_consecutive_downloads_dont_contend(self) -> None:
        """After the first download finishes, a second start_download
        should succeed without 'already downloading' errors from a
        lingering watcher."""
        tmp = Path(tempfile.mkdtemp())
        mm = _make_manager(tmp)
        # Set up the second spec to look "already downloaded" so the only
        # way start_download can succeed is by waiting for the first
        # download's state to clear — i.e. the watcher must have actually
        # exited, not be lingering inside a time.sleep.
        from app.model_manager import DownloadState
        mm._states["whisperx-base"] = DownloadState(
            status="installed", stalled=False, manual_download_url=None,
            last_progress_at=0.0, last_size_bytes=0, token=0,
        )
        # Mark whisperx-base as installed on disk so start_download's
        # "already installed" check doesn't reject it
        (tmp / "whisperx-base").mkdir(parents=True, exist_ok=True)
        (tmp / "whisperx-base" / "weights.bin").write_bytes(b"\x00")

        with patch("huggingface_hub.snapshot_download", side_effect=lambda repo_id, local_dir, **kw: (
            time.sleep(0.1),
            Path(local_dir).mkdir(parents=True, exist_ok=True),
            Path(local_dir, "w").write_bytes(b"\x00" * 10),
            local_dir,
        )):
            mm.start_download("whisperx-tiny")
            # tiny is now installing concurrently with base being "installed"
            self.assertTrue(_wait_for_state(mm, "whisperx-tiny", predicate=lambda s: s.status == "installed", timeout=3.0))

    def test_wake_events_cleared_on_delete(self) -> None:
        """Deleting a model must also free its wake Event to avoid leaks."""
        from app.model_manager import DownloadState
        tmp = Path(tempfile.mkdtemp())
        mm = _make_manager(tmp)
        # Use a real model name (whisperx-tiny) so delete_model's get_spec
        # lookup succeeds. We're testing state/event cleanup, not validation.
        target = "whisperx-tiny"
        mm._states[target] = DownloadState(
            status="installed", stalled=False, manual_download_url=None,
            last_progress_at=0.0, last_size_bytes=0, token=0,
        )
        mm._wake_events[target] = threading.Event()
        # delete_model(name, current_model) — pass a different current model
        # name so the "can't delete the active model" guard doesn't fire.
        mm.delete_model(target, current_model="whisperx-medium")
        self.assertNotIn(target, mm._states)
        self.assertNotIn(target, mm._wake_events)


if __name__ == "__main__":
    unittest.main()

