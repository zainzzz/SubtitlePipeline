from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import Database


class _EnsureColumnFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_dir = self.base / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.database = Database(str(self.config_dir / "test.db"))
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()


class EnsureColumnAllowlistTests(_EnsureColumnFixture):
    """P1-7: _ensure_column must reject unknown table names (SQL injection guard)."""

    def test_injection_attempt_raises_value_error(self) -> None:
        evil_name = "evil_table; DROP TABLE tasks"
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                self.database._ensure_column(conn, evil_name, "col", "TEXT")
        finally:
            conn.close()

    def test_valid_table_names_work(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        conn.execute("CREATE TABLE files (id INTEGER)")
        conn.execute("CREATE TABLE task_logs (id INTEGER)")
        try:
            self.database._ensure_column(conn, "tasks", "new_col", "TEXT NOT NULL DEFAULT ''")
            self.database._ensure_column(conn, "files", "new_col", "INTEGER NOT NULL DEFAULT 0")
            self.database._ensure_column(conn, "task_logs", "new_col", "TEXT")
            cols_tasks = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            cols_files = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
            cols_logs = {row[1] for row in conn.execute("PRAGMA table_info(task_logs)").fetchall()}
            self.assertIn("new_col", cols_tasks)
            self.assertIn("new_col", cols_files)
            self.assertIn("new_col", cols_logs)
        finally:
            conn.close()

    def test_column_name_cannot_be_used_for_injection(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        try:
            self.database._ensure_column(conn, "tasks", "col1", "TEXT")
            with self.assertRaises(ValueError):
                self.database._ensure_column(
                    conn, "tasks", "col2; DROP TABLE tasks", "TEXT"
                )
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchall()
            self.assertEqual(len(tables), 1, "tasks table should still exist")
        finally:
            conn.close()

    def test_ensure_column_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        try:
            self.database._ensure_column(conn, "tasks", "dup_col", "TEXT")
            self.database._ensure_column(conn, "tasks", "dup_col", "TEXT")
            cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            self.assertEqual(cols.count("dup_col"), 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
