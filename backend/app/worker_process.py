"""Worker process entry point.

Connects to the shared SQLite database, processes subtitle generation tasks,
and handles graceful shutdown on SIGTERM / SIGINT.

On startup, orphaned tasks from a previous (crashed/stopped) run are
recovered via ``database.recover_orphaned_tasks()``.  On shutdown the
current task is allowed to complete before the process exits.
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from .logging_utils import setup_logging
from .main import resolve_db_path
from .runtime import WorkerService
from .store import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful shutdown support
# ---------------------------------------------------------------------------
# Module-level flag inspected between task iterations.  When a SIGTERM or
# SIGINT arrives the handler sets this to ``True``.  The main loop checks the
# flag *between* tasks, so the current task runs to completion before exit
# (cooperative / graceful shutdown).
_should_stop = False


class _GracefulExit(Exception):
    """Raised by the signal handler to break out of blocking ``time.sleep``.

    Under PEP 475 (Python ≥ 3.5) a bare signal handler that merely sets a flag
    does NOT interrupt ``time.sleep`` — the syscall is retried.  Raising this
    exception forces immediate exit from any blocking call so the process
    responds to ``docker stop`` within seconds rather than waiting for the
    next poll interval.
    """


def _handle_signal(signum: int, frame) -> None:  # noqa: ANN001
    """SIGTERM / SIGINT handler — set flag and interrupt blocking calls."""
    global _should_stop
    _should_stop = True
    logger.info("Worker received signal %d, initiating graceful shutdown", signum)
    raise _GracefulExit()


def _register_signal_handlers() -> None:
    """Install SIGTERM / SIGINT handlers (idempotent, safe to call in tests)."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def main() -> None:
    _register_signal_handlers()

    database = Database(resolve_db_path(), persistent=True)
    database.initialize()
    setup_logging(database)
    database.recover_orphaned_tasks()
    database.clear_restart_required()

    service = WorkerService(database)
    try:
        service.run_forever()
    except _GracefulExit:
        logger.info("Worker graceful shutdown complete (flag=%s)", _should_stop)
        sys.exit(0)


if __name__ == "__main__":
    main()
