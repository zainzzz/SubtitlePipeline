"""Schema initialization and migration logic, extracted from `Database`.

Owns:
- `initialize()` — schema bootstrap + idempotent migrations
- `_ensure_column()` — safe `ALTER TABLE ADD COLUMN` with strict allowlist
- `OBSOLETE_CONFIG_FIELDS` cleanup
- Default config seeding + `system.setup_complete` / `system.config_version` rows

The `DatabaseMigrations` class is constructed with a reference to its parent
`Database` instance and delegates connection management back to it. This keeps
the public `app.store.Database` API unchanged while moving the schema/config
bootstrap code into a focused module.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import TYPE_CHECKING, Any

from ..defaults import SYSTEM_LEVEL_FIELDS, copy_default_config

if TYPE_CHECKING:
    from . import Database

logger = logging.getLogger(__name__)


# Subset of OBSOLETE_CONFIG_FIELDS relevant to migrations; kept here so the
# migrations module is self-contained. Re-exported from ``store.__init__``.
OBSOLETE_CONFIG_FIELDS: set[tuple[str, str]] = {
    ("file", "in_place"),
    ("file", "output_dir"),
    ("processing", "backend_mode"),
    ("scanner", "max_pending_tasks"),
    ("translation", "provider"),
    ("translation", "mock_prefix_template"),
    ("translation", "fail_languages"),
    ("subtitle", "text_process_style"),
    ("whisper", "align_model"),
    ("whisper", "align_method"),
    ("mux", "output_dir"),
    ("logging", "page_size"),
}


def _load_config_value(connection: sqlite3.Connection, group_name: str, key_name: str) -> Any | None:
    row = connection.execute(
        """
        SELECT value_json
        FROM system_config
        WHERE group_name = ? AND key_name = ?
        """,
        (group_name, key_name),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["value_json"])


def _normalize_legacy_align_method(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    legacy_mapping = {
        "whisperx": "auto",
        "auto": "auto",
        "simple": "none",
        "none": "none",
    }
    return legacy_mapping.get(normalized, normalized or "auto")


class DatabaseMigrations:
    """Owns schema init + migrations for the SubtitlePipeline SQLite store.

    Constructed with a back-reference to its parent `Database` so it can reuse
    the parent's `connect()` context manager. All schema bootstrap, column
    addition, and obsolete-field cleanup live here.
    """

    # Allowlist for _ensure_column — defends against SQL injection via
    # attacker-controlled table names. Pinned in test_p1_7_ensure_column.
    _ALLOWED_TABLE_NAMES = frozenset({"tasks", "files", "task_logs"})

    def __init__(self, database: "Database") -> None:
        self.database = database

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Create all tables and perform idempotent schema/data migrations.

        Safe to call repeatedly: uses `CREATE TABLE IF NOT EXISTS`,
        `ON CONFLICT DO NOTHING`, and `_ensure_column` for additive migrations.
        """
        from . import utc_now  # local import to avoid circular at module load

        with self.database.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    path_key TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    stable_hits INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER,
                    file_path TEXT NOT NULL,
                    file_path_key TEXT NOT NULL,
                    source_size_bytes INTEGER NOT NULL DEFAULT 0,
                    source_mtime REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    restart_required INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    config_snapshot TEXT,
                    result_payload TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    parent_dir_ctime REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(file_id) REFERENCES files(id)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status_created_at ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_file_path_key_status ON tasks(file_path_key, status);

                CREATE TABLE IF NOT EXISTS task_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_logs_task_time ON task_logs(task_id, timestamp, id);

                CREATE TABLE IF NOT EXISTS system_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    key_name TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    restart_required INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(group_name, key_name)
                );

                CREATE TABLE IF NOT EXISTS translation_cache (
                    source_hash TEXT NOT NULL,
                    target_language TEXT NOT NULL,
                    llm_type TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    custom_prompt_hash TEXT NOT NULL,
                    translated_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source_hash, target_language, llm_type, model, content_type, custom_prompt_hash)
                );

                CREATE TABLE IF NOT EXISTS scan_status (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_scan_at TEXT,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    queued INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    pending_count INTEGER NOT NULL DEFAULT 0,
                    throttled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(connection, "tasks", "source_size_bytes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tasks", "source_mtime", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(connection, "tasks", "parent_dir_ctime", "REAL NOT NULL DEFAULT 0")
            now = utc_now()
            in_place = bool(_load_config_value(connection, "file", "in_place"))
            legacy_file_output_dir = _load_config_value(connection, "file", "output_dir")
            legacy_mux_output_dir = _load_config_value(connection, "mux", "output_dir")
            output_to_source_dir = _load_config_value(connection, "file", "output_to_source_dir")
            has_legacy_output_fields = any(
                value is not None
                for value in (
                    legacy_file_output_dir,
                    legacy_mux_output_dir,
                    _load_config_value(connection, "file", "in_place"),
                )
            )
            if output_to_source_dir is None and has_legacy_output_fields:
                migrated_output_to_source_dir = (
                    in_place
                    or (
                        str(legacy_file_output_dir or "").strip() == ""
                        and str(legacy_mux_output_dir or "").strip() == ""
                    )
                )
                connection.execute(
                    """
                    INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                    VALUES ('file', 'output_to_source_dir', ?, 'runtime', 0, ?)
                    ON CONFLICT(group_name, key_name)
                    DO UPDATE SET value_json = excluded.value_json,
                                  updated_at = excluded.updated_at
                    """,
                    (json.dumps(migrated_output_to_source_dir), now),
                )
            legacy_align_method = _load_config_value(connection, "whisper", "align_method")
            align_provider = _load_config_value(connection, "whisper", "align_provider")
            if legacy_align_method is not None and align_provider is None:
                connection.execute(
                    """
                    INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                    VALUES ('whisper', 'align_provider', ?, 'runtime', 0, ?)
                    ON CONFLICT(group_name, key_name)
                    DO UPDATE SET value_json = excluded.value_json,
                                  updated_at = excluded.updated_at
                    """,
                    (json.dumps(_normalize_legacy_align_method(legacy_align_method)), now),
                )
            connection.execute(
                """
                UPDATE tasks
                SET source_size_bytes = COALESCE(source_size_bytes, 0),
                    source_mtime = COALESCE(source_mtime, 0)
                """
            )
            connection.executemany(
                """
                DELETE FROM system_config
                WHERE group_name = ? AND key_name = ?
                """,
                list(OBSOLETE_CONFIG_FIELDS),
            )
            defaults = copy_default_config()
            for group_name, group_values in defaults.items():
                for key_name, value in group_values.items():
                    scope = "system" if (group_name, key_name) in SYSTEM_LEVEL_FIELDS else "runtime"
                    restart_required = 1 if scope == "system" else 0
                    connection.execute(
                        """
                        INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(group_name, key_name) DO NOTHING
                        """,
                        (group_name, key_name, json.dumps(value), scope, restart_required, now),
                    )
            connection.execute(
                """
                INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                VALUES ('system', 'setup_complete', 'false', 'system', 0, ?)
                ON CONFLICT(group_name, key_name) DO NOTHING
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                VALUES ('system', 'config_version', '0', 'system', 0, ?)
                ON CONFLICT(group_name, key_name) DO NOTHING
                """,
                (now,),
            )

    # ------------------------------------------------------------------
    # Safe column addition
    # ------------------------------------------------------------------

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        """Add a column to a table if (and only if) it does not already exist.

        Hardened against SQL injection:
        - `table_name` must appear in `_ALLOWED_TABLE_NAMES`.
        - `column_name` must match `^[A-Za-z_][A-Za-z0-9_]*$`.
        - `definition` must match `^[A-Za-z0-9 ()'_]*$`.

        Idempotent: calling twice with the same args results in a single column.
        """
        if table_name not in self._ALLOWED_TABLE_NAMES:
            raise ValueError(f"unexpected table name for _ensure_column: {table_name!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column_name):
            raise ValueError(f"unexpected column name for _ensure_column: {column_name!r}")
        if not re.fullmatch(r"[A-Za-z0-9 ()'_]*", definition):
            raise ValueError(f"unexpected column definition for _ensure_column: {definition!r}")
        # PRAGMA table_info column index 1 is the name (sqlite3.Row agnostic).
        columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(column[1] == column_name for column in columns):
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    # ------------------------------------------------------------------
    # Whisper config migration helper (extracted from module-level)
    # ------------------------------------------------------------------

    @staticmethod
    def _migrate_whisper_config_dict(whisper_config: dict[str, Any]) -> None:
        """Normalize legacy `align_method` → `align_provider` in place.

        Idempotent: a no-op once `align_provider` is already present.
        """
        from .config import _normalize_align_provider, _normalize_legacy_align_method

        if "align_provider" in whisper_config:
            whisper_config["align_provider"] = _normalize_align_provider(whisper_config.get("align_provider"))
        elif "align_method" in whisper_config:
            whisper_config["align_provider"] = _normalize_legacy_align_method(whisper_config.get("align_method"))
        whisper_config.pop("align_method", None)
