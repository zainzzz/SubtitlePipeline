"""Tests for graceful shutdown of scanner_process and worker_process.

Verifies that SIGTERM / SIGINT set the ``_should_stop`` flag, that the
signal handler raises ``_GracefulExit`` to interrupt blocking calls, and
that a loop checking the flag exits cleanly between tasks (i.e. the
current task is allowed to finish).
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestScannerGracefulShutdown(unittest.TestCase):
    """Tests for backend.app.scanner_process signal handling."""

    def setUp(self) -> None:
        # Ensure flag is reset before each test.
        from app import scanner_process
        scanner_process._should_stop = False

    def test_sigterm_sets_should_stop(self) -> None:
        """Sending SIGTERM sets the global _should_stop flag."""
        from app import scanner_process

        self.assertFalse(scanner_process._should_stop)
        # Call handler directly (simulates OS signal delivery).
        scanner_process._handle_signal(signal.SIGTERM, None)
        self.assertTrue(scanner_process._should_stop)

    def test_sigint_sets_should_stop(self) -> None:
        """SIGINT also sets the flag."""
        from app import scanner_process

        self.assertFalse(scanner_process._should_stop)
        scanner_process._handle_signal(signal.SIGINT, None)
        self.assertTrue(scanner_process._should_stop)

    def test_signal_handler_raises_graceful_exit(self) -> None:
        """The handler raises _GracefulExit to interrupt blocking sleep."""
        from app import scanner_process

        with self.assertRaises(scanner_process._GracefulExit):
            scanner_process._handle_signal(signal.SIGTERM, None)

    def test_main_loop_exits_on_flag(self) -> None:
        """The main() function catches _GracefulExit and exits cleanly."""
        from app import scanner_process

        # Simulate: service.run_forever raises _GracefulExit
        mock_service = MagicMock()
        mock_service.run_forever.side_effect = scanner_process._GracefulExit()

        with patch.object(scanner_process, "Database") as mock_db_cls, \
             patch.object(scanner_process, "setup_logging"), \
             patch.object(scanner_process, "ScannerService", return_value=mock_service), \
             patch.object(scanner_process, "resolve_db_path", return_value="/tmp/test.db"), \
             patch.object(scanner_process, "_register_signal_handlers"), \
             patch("sys.exit") as mock_exit:
            scanner_process.main()
            mock_exit.assert_called_once_with(0)
            mock_service.run_forever.assert_called_once()


class TestWorkerGracefulShutdown(unittest.TestCase):
    """Tests for backend.app.worker_process signal handling."""

    def setUp(self) -> None:
        from app import worker_process
        worker_process._should_stop = False

    def test_sigterm_sets_should_stop(self) -> None:
        """Sending SIGTERM sets the global _should_stop flag."""
        from app import worker_process

        self.assertFalse(worker_process._should_stop)
        worker_process._handle_signal(signal.SIGTERM, None)
        self.assertTrue(worker_process._should_stop)

    def test_sigint_sets_should_stop(self) -> None:
        """SIGINT also sets the flag."""
        from app import worker_process

        self.assertFalse(worker_process._should_stop)
        worker_process._handle_signal(signal.SIGINT, None)
        self.assertTrue(worker_process._should_stop)

    def test_signal_handler_raises_graceful_exit(self) -> None:
        """The handler raises _GracefulExit to interrupt blocking sleep."""
        from app import worker_process

        with self.assertRaises(worker_process._GracefulExit):
            worker_process._handle_signal(signal.SIGTERM, None)

    def test_main_loop_exits_on_flag(self) -> None:
        """The main() function catches _GracefulExit and exits cleanly."""
        from app import worker_process

        mock_service = MagicMock()
        mock_service.run_forever.side_effect = worker_process._GracefulExit()

        with patch.object(worker_process, "Database") as mock_db_cls, \
             patch.object(worker_process, "setup_logging"), \
             patch.object(worker_process, "WorkerService", return_value=mock_service), \
             patch.object(worker_process, "resolve_db_path", return_value="/tmp/test.db"), \
             patch.object(worker_process, "_register_signal_handlers"), \
             patch("sys.exit") as mock_exit:
            worker_process.main()
            mock_exit.assert_called_once_with(0)
            mock_service.run_forever.assert_called_once()

    def test_graceful_shutdown_waits_for_current_task(self) -> None:
        """A loop that checks _should_stop between tasks finishes the
        current task before exiting.

        This simulates the pattern used by the worker: process_next_task()
        runs to completion, then the flag is checked.  If the signal arrives
        *during* the task, the task still finishes.
        """
        from app import worker_process

        task_completed = []

        def mock_process_next_task() -> bool:
            # Simulate a task that takes a moment.
            time.sleep(0.05)
            task_completed.append(True)
            return True

        # Simulate the worker loop pattern.
        iterations = 0
        while not worker_process._should_stop and iterations < 3:
            iterations += 1
            mock_process_next_task()
            # Signal arrives after the first task.
            if iterations == 1:
                worker_process._should_stop = True

        # The task that was in progress when the signal was set should complete.
        self.assertGreaterEqual(len(task_completed), 1)
        # The loop should exit after the current task finishes.
        self.assertEqual(iterations, 2)


class TestEndToEndSignalDelivery(unittest.TestCase):
    """Verify signal handler is registered via _register_signal_handlers."""

    def test_register_signal_handlers_sets_handlers(self) -> None:
        """_register_signal_handlers installs SIGTERM and SIGINT handlers."""
        from app import scanner_process

        # Save existing handlers.
        old_term = signal.getsignal(signal.SIGTERM)
        old_int = signal.getsignal(signal.SIGINT)
        try:
            scanner_process._register_signal_handlers()
            # Handlers should be our _handle_signal function.
            self.assertEqual(signal.getsignal(signal.SIGTERM), scanner_process._handle_signal)
            self.assertEqual(signal.getsignal(signal.SIGINT), scanner_process._handle_signal)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)


if __name__ == "__main__":
    unittest.main()
