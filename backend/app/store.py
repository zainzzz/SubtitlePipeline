from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

from .defaults import RESULT_AFFECTING_GROUPS, SYSTEM_LEVEL_FIELDS, copy_default_config, detect_device
from .pipeline import (
    check_resume_feasibility,
    cleanup_intermediates,
    cleanup_work_dir_intermediates,
    normalize_stage_name,
)


OBSOLETE_CONFIG_FIELDS = {
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

READ_ONLY_CONFIG_FIELDS = {
    ("whisper", "device"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_path(value: str) -> str:
    return str(Path(value).expanduser().resolve()).lower()


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


def _normalize_align_provider(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    supported = {"auto", "whisperx", "qwen-forced", "none"}
    return normalized if normalized in supported else "auto"


def _migrate_whisper_config_dict(whisper_config: dict[str, Any]) -> None:
    if "align_provider" in whisper_config:
        whisper_config["align_provider"] = _normalize_align_provider(whisper_config.get("align_provider"))
    elif "align_method" in whisper_config:
        whisper_config["align_provider"] = _normalize_legacy_align_method(whisper_config.get("align_method"))
    whisper_config.pop("align_method", None)


def _migrate_notification_config(notification_config: dict[str, Any]) -> None:
    """Backfill defaults for new notification fields on existing config rows.

    New in 2026-08: `trigger_on_subtitle_change` and `subtitle_change_debounce_seconds`.
    Both are read by the API when handling subtitle edit endpoints, so missing
    values would silently disable the feature for users on an old config row.
    """
    notification_config.setdefault("trigger_on_subtitle_change", True)
    debounce = notification_config.get("subtitle_change_debounce_seconds")
    if debounce is None or not isinstance(debounce, (int, float)) or debounce < 0:
        notification_config["subtitle_change_debounce_seconds"] = 5


def _migrate_translation_config(translation_config: dict[str, Any]) -> None:
    """Backfill defaults for new translation sampling / retry fields.

    New in 2026-08: `temperature`, `max_tokens`, `frequency_penalty`,
    `presence_penalty`, `http_max_retries`. Previously these were hardcoded
    constants in the LLM clients — users could not tune them. We default
    to the previous hardcoded values so existing users see no behavior change.
    """
    if not isinstance(translation_config.get("temperature"), (int, float)):
        translation_config["temperature"] = 0.3
    if not isinstance(translation_config.get("max_tokens"), int) or translation_config["max_tokens"] <= 0:
        translation_config["max_tokens"] = 8192
    if not isinstance(translation_config.get("frequency_penalty"), (int, float)):
        translation_config["frequency_penalty"] = 1.2
    if not isinstance(translation_config.get("presence_penalty"), (int, float)):
        translation_config["presence_penalty"] = 0.8
    if not isinstance(translation_config.get("http_max_retries"), int) or translation_config["http_max_retries"] < 0:
        translation_config["http_max_retries"] = 3


def _migrate_quality_config(quality_config: dict[str, Any]) -> None:
    """Backfill defaults for the new `quality` config block (2026-08).

    All fields are tuning knobs; a missing block means "use defaults" so we
    populate the entire dict rather than cherry-pick keys.
    """
    defaults = {
        "enabled": True,
        "min_avg_confidence": 0.6,
        "min_segment_duration": 0.8,
        "max_segment_duration": 12.0,
        "max_repeat_segments": 3,
        "min_translation_char_ratio": 0.2,
        "max_translation_char_ratio": 3.0,
        "suspect_score_threshold": 80,
    }
    for key, default in defaults.items():
        if key not in quality_config or quality_config[key] is None:
            quality_config[key] = default


def _migrate_processing_config(processing_config: dict[str, Any]) -> None:
    """Backfill defaults for the new pre-ASR resource check fields."""
    if not isinstance(processing_config.get("pre_asr_resource_check"), bool):
        processing_config["pre_asr_resource_check"] = True
    if not isinstance(processing_config.get("resource_headroom_pct"), (int, float)):
        processing_config["resource_headroom_pct"] = 20
    elif not (0 <= float(processing_config["resource_headroom_pct"]) <= 80):
        processing_config["resource_headroom_pct"] = 20


@dataclass
class PageResult:
    items: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    status_counts: dict[str, int]


class Database:
    def __init__(self, db_path: str, persistent: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.persistent = persistent
        self._conn = self._create_connection() if persistent else None
        self._config_cache: dict[str, Any] | None = None
        self._cache_valid = False

    def _create_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._conn is not None:
            yield self._conn
            self._conn.commit()
            return
        connection = self._create_connection()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None

    def initialize(self) -> None:
        with self.connect() as connection:
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
                for value in (legacy_file_output_dir, legacy_mux_output_dir, _load_config_value(connection, "file", "in_place"))
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

    def _build_result_affecting_snapshot(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            group_name: group_values
            for group_name, group_values in config.items()
            if group_name in RESULT_AFFECTING_GROUPS
        }

    def _resolve_task_work_dir(self, task_id: int, config_snapshot: dict[str, Any] | None) -> Path:
        if config_snapshot is not None:
            return Path(config_snapshot["processing"]["work_dir"]) / str(task_id)
        config = self.get_config()
        return Path(config["processing"]["work_dir"]) / str(task_id)

    def _ensure_column(self, connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        if any(column["name"] == column_name for column in columns):
            return
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def _get_config_uncached(self) -> dict[str, Any]:
        defaults = copy_default_config()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT group_name, key_name, value_json, scope, restart_required, updated_at
                FROM system_config
                ORDER BY group_name, key_name
                """
            ).fetchall()
        restart_required = False
        for row in rows:
            restart_required = restart_required or bool(row["restart_required"] and row["scope"] == "system")
            if row["group_name"] == "system":
                continue
            defaults.setdefault(row["group_name"], {})[row["key_name"]] = json.loads(row["value_json"])
        whisper_config = defaults.setdefault("whisper", {})
        _migrate_whisper_config_dict(whisper_config)
        notification_config = defaults.setdefault("notification", {})
        _migrate_notification_config(notification_config)
        translation_config = defaults.setdefault("translation", {})
        _migrate_translation_config(translation_config)
        quality_config = defaults.setdefault("quality", {})
        _migrate_quality_config(quality_config)
        processing_config = defaults.setdefault("processing", {})
        _migrate_processing_config(processing_config)
        if defaults.get("whisper", {}).get("device") == "auto":
            defaults["whisper"]["device"] = detect_device()
        defaults["meta"] = {"restart_required": restart_required}
        return defaults

    def get_config(self) -> dict[str, Any]:
        if self._cache_valid and self._config_cache is not None:
            return self._config_cache
        self._config_cache = self._get_config_uncached()
        self._cache_valid = True
        return self._config_cache

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        updated_system_key = False
        with self.connect() as connection:
            current = self._get_config_uncached()
            now = utc_now()
            for group_name, group_values in payload.items():
                if not isinstance(group_values, dict):
                    continue
                for key_name, value in group_values.items():
                    if group_name == "whisper" and key_name == "align_method":
                        key_name = "align_provider"
                        value = _normalize_legacy_align_method(value)
                    elif group_name == "whisper" and key_name == "align_provider":
                        value = _normalize_align_provider(value)
                    if (group_name, key_name) in READ_ONLY_CONFIG_FIELDS:
                        continue
                    if group_name not in current or key_name not in current[group_name]:
                        raise KeyError(f"unknown config field: {group_name}.{key_name}")
                    scope = "system" if (group_name, key_name) in SYSTEM_LEVEL_FIELDS else "runtime"
                    restart_required = 1 if scope == "system" else 0
                    if scope == "system" and current[group_name][key_name] != value:
                        updated_system_key = True
                    connection.execute(
                        """
                        INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(group_name, key_name)
                        DO UPDATE SET value_json = excluded.value_json,
                                      scope = excluded.scope,
                                      restart_required = excluded.restart_required,
                                      updated_at = excluded.updated_at
                        """,
                        (group_name, key_name, json.dumps(value), scope, restart_required, now),
                    )
            if updated_system_key:
                connection.execute(
                    """
                    UPDATE system_config
                    SET restart_required = CASE WHEN scope = 'system' THEN 1 ELSE restart_required END,
                        updated_at = ?
                    """,
                    (now,),
                )
        self._cache_valid = False
        return self.get_config()

    def recover_orphaned_tasks(self) -> int:
        """Reset tasks stuck in 'processing' (e.g. after a crash) to 'failed' so users can retry."""
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    error_message = '系统重启，任务中断',
                    updated_at = ?,
                    finished_at = ?
                WHERE status = 'processing'
                """,
                (now, now),
            )
            count = cursor.rowcount
        if count:
            logger.warning("系统启动: 已将 %d 个中断的 processing 任务标记为 failed", count)
        return count

    def clear_restart_required(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE system_config SET restart_required = 0 WHERE scope = 'system'"
            )

    def is_setup_complete(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT value_json
                FROM system_config
                WHERE group_name = 'system' AND key_name = 'setup_complete'
                """
            ).fetchone()
        return bool(json.loads(row["value_json"])) if row else False

    def get_system_status(self) -> dict[str, Any]:
        config = self.get_config()
        translation = config["translation"]
        translation_ready = True
        if translation["enabled"]:
            required_keys = ("api_base_url", "model") if translation.get("llm_type") in {"lmstudio", "ollama"} else ("api_base_url", "api_key", "model")
            translation_ready = all(
                str(translation[key]).strip()
                for key in required_keys
            )
        return {
            "setup_complete": self.is_setup_complete(),
            "translation_ready": translation_ready,
        }

    def set_setup_complete(self, setup_complete: bool) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                VALUES ('system', 'setup_complete', ?, 'system', 0, ?)
                ON CONFLICT(group_name, key_name)
                DO UPDATE SET value_json = excluded.value_json,
                              updated_at = excluded.updated_at,
                              restart_required = 0
                """,
                (json.dumps(setup_complete), utc_now()),
            )
        return self.get_system_status()

    def list_tasks(self, page: int, page_size: int, status: str | None = None) -> PageResult:
        offset = max(page - 1, 0) * page_size
        filters: list[Any] = []
        where_clause = ""
        if status:
            where_clause = "WHERE status = ?"
            filters.append(status)
        # 已完成/失败按更新时间排序，其余按文件夹创建时间排序
        if status in ("done", "failed"):
            order_clause = "ORDER BY updated_at DESC, id DESC"
        else:
            order_clause = "ORDER BY parent_dir_ctime DESC, id DESC"
        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM tasks {where_clause}",
                filters,
            ).fetchone()[0]
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks
                GROUP BY status
                """
            ).fetchall()
            rows = connection.execute(
                f"""
                SELECT id, file_path, status, stage, progress, retry_count, max_retries, cancel_requested,
                       restart_required, error_message, created_at, updated_at, started_at, finished_at
                FROM tasks
                {where_clause}
                {order_clause}
                LIMIT ? OFFSET ?
                """,
                [*filters, page_size, offset],
            ).fetchall()
        return PageResult(
            [
                {
                    **dict(row),
                    "stage": normalize_stage_name(str(row["stage"])),
                }
                for row in rows
            ],
            page,
            page_size,
            total,
            {str(row["status"]): int(row["count"]) for row in status_rows},
        )

    def count_tasks_by_status(self, status: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE status = ?
                """,
                (status,),
            ).fetchone()
        return int(row[0]) if row else 0

    def status_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, file_path, status, stage, progress, retry_count, max_retries, cancel_requested,
                       restart_required, error_message, config_snapshot, result_payload,
                       created_at, updated_at, started_at, finished_at
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        if not row:
            return None
        task = dict(row)
        task["stage"] = normalize_stage_name(str(task["stage"]))
        task["config_snapshot"] = json.loads(task["config_snapshot"]) if task["config_snapshot"] else None
        task["result_payload"] = json.loads(task["result_payload"]) if task["result_payload"] else None
        if task["config_snapshot"] and isinstance(task["config_snapshot"].get("whisper"), dict):
            _migrate_whisper_config_dict(task["config_snapshot"]["whisper"])
        if task["config_snapshot"] and isinstance(task["config_snapshot"].get("notification"), dict):
            _migrate_notification_config(task["config_snapshot"]["notification"])
        if task["config_snapshot"] and isinstance(task["config_snapshot"].get("translation"), dict):
            _migrate_translation_config(task["config_snapshot"]["translation"])
        if task["config_snapshot"] and isinstance(task["config_snapshot"].get("quality"), dict):
            _migrate_quality_config(task["config_snapshot"]["quality"])
        if task["config_snapshot"] and isinstance(task["config_snapshot"].get("processing"), dict):
            _migrate_processing_config(task["config_snapshot"]["processing"])
        return task

    def get_logs(self, task_id: int, page: int, page_size: int) -> PageResult:
        offset = max(page - 1, 0) * page_size
        with self.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM task_logs WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT id, task_id, stage, level, message, details_json, timestamp
                FROM task_logs
                WHERE task_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (task_id, page_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details_json"]) if item["details_json"] else None
            item.pop("details_json", None)
            items.append(item)
        return PageResult(items, page, page_size, total, {})

    def log(self, task_id: int, stage: str, level: str, message: str, details: dict[str, Any] | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO task_logs (task_id, stage, level, message, details_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, stage, level, message, json.dumps(details) if details else None, utc_now()),
            )

    def request_cancel(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET cancel_requested = 1, updated_at = ?
                WHERE id = ? AND status = 'processing'
                """,
                (utc_now(), task_id),
            )
        return self.get_task(task_id)

    def request_retry(self, task_id: int, mode: str = "restart") -> dict[str, Any] | None:
        if mode not in {"restart", "resume"}:
            raise ValueError("不支持的重试模式")
        task = self.get_task(task_id)
        if not task or task["status"] not in {"failed", "cancelled", "done"}:
            return None
        if mode == "resume":
            feasibility = check_resume_feasibility(task)
            if not feasibility["can_resume"]:
                missing = ", ".join(feasibility["missing"])
                raise ValueError(f"中间文件缺失，无法继续执行: {missing}")
            resume_stage = str(feasibility.get("resume_stage", normalize_stage_name(task["stage"])))
        else:
            cleanup_intermediates(Path(task["file_path"]))
            cleanup_work_dir_intermediates(self._resolve_task_work_dir(task_id, task["config_snapshot"]))
            resume_stage = "queued"
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'pending',
                    stage = ?,
                    progress = ?,
                    cancel_requested = 0,
                    error_message = NULL,
                    result_payload = NULL,
                    updated_at = ?,
                    started_at = NULL,
                    finished_at = NULL
                WHERE id = ? AND status IN ('failed', 'cancelled', 'done')
                """,
                (
                    resume_stage,
                    0 if mode == "restart" else float(task["progress"]),
                    utc_now(),
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def observe_file(self, file_path: str, size_bytes: int, mtime: float) -> dict[str, Any]:
        path_key = normalize_path(file_path)
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id, size_bytes, mtime, stable_hits
                FROM files
                WHERE path_key = ?
                """,
                (path_key,),
            ).fetchone()
            if existing:
                stable_hits = existing["stable_hits"] + 1 if (
                    existing["size_bytes"] == size_bytes and float(existing["mtime"]) == float(mtime)
                ) else 1
                connection.execute(
                    """
                    UPDATE files
                    SET path = ?, size_bytes = ?, mtime = ?, stable_hits = ?, last_seen_at = ?
                    WHERE path_key = ?
                    """,
                    (file_path, size_bytes, mtime, stable_hits, now, path_key),
                )
                file_id = existing["id"]
            else:
                stable_hits = 1
                cursor = connection.execute(
                    """
                    INSERT INTO files (path, path_key, size_bytes, mtime, stable_hits, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (file_path, path_key, size_bytes, mtime, stable_hits, now, now),
                )
                file_id = cursor.lastrowid
        return {
            "file_id": file_id,
            "path": file_path,
            "path_key": path_key,
            "size_bytes": size_bytes,
            "mtime": mtime,
            "stable_hits": stable_hits,
        }

    def bulk_observe_files(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Upsert many files in one transaction.

        Args:
            items: list of {"file_path", "size_bytes", "mtime"} dicts.

        Returns:
            dict mapping path_key -> {file_id, path, path_key, size_bytes, mtime, stable_hits}.
            Items already in the DB get stable_hits incremented (same logic as observe_file);
            new items are inserted with stable_hits=1.
        """
        if not items:
            return {}
        result: dict[str, dict[str, Any]] = {}
        now = utc_now()
        # Process in chunks to avoid one giant transaction on big libraries
        chunk_size = 200
        for start in range(0, len(items), chunk_size):
            chunk = items[start:start + chunk_size]
            path_keys = [normalize_path(it["file_path"]) for it in chunk]
            with self.connect() as connection:
                placeholders = ",".join("?" for _ in path_keys)
                rows = connection.execute(
                    f"SELECT id, path_key, size_bytes, mtime, stable_hits FROM files WHERE path_key IN ({placeholders})",
                    path_keys,
                ).fetchall()
                existing = {r["path_key"]: r for r in rows}
                for it in chunk:
                    path_key = normalize_path(it["file_path"])
                    size = int(it["size_bytes"])
                    mtime = float(it["mtime"])
                    row = existing.get(path_key)
                    if row:
                        stable_hits = row["stable_hits"] + 1 if (
                            row["size_bytes"] == size and float(row["mtime"]) == mtime
                        ) else 1
                        connection.execute(
                            """
                            UPDATE files
                            SET path = ?, size_bytes = ?, mtime = ?, stable_hits = ?, last_seen_at = ?
                            WHERE id = ?
                            """,
                            (it["file_path"], size, mtime, stable_hits, now, row["id"]),
                        )
                        result[path_key] = {
                            "file_id": row["id"],
                            "path": it["file_path"],
                            "path_key": path_key,
                            "size_bytes": size,
                            "mtime": mtime,
                            "stable_hits": stable_hits,
                        }
                    else:
                        cursor = connection.execute(
                            """
                            INSERT INTO files (path, path_key, size_bytes, mtime, stable_hits, first_seen_at, last_seen_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (it["file_path"], path_key, size, mtime, 1, now, now),
                        )
                        result[path_key] = {
                            "file_id": cursor.lastrowid,
                            "path": it["file_path"],
                            "path_key": path_key,
                            "size_bytes": size,
                            "mtime": mtime,
                            "stable_hits": 1,
                        }
        return result

    def get_translation_cache(
        self,
        source_hash: str,
        target_language: str,
        llm_type: str,
        model: str,
        content_type: str,
        custom_prompt_hash: str,
    ) -> list[str] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT translated_json
                FROM translation_cache
                WHERE source_hash = ?
                  AND target_language = ?
                  AND llm_type = ?
                  AND model = ?
                  AND content_type = ?
                  AND custom_prompt_hash = ?
                LIMIT 1
                """,
                (source_hash, target_language, llm_type, model, content_type, custom_prompt_hash),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["translated_json"])
        if not isinstance(value, list):
            return None
        return [str(item) for item in value]

    def set_translation_cache(
        self,
        source_hash: str,
        target_language: str,
        llm_type: str,
        model: str,
        content_type: str,
        custom_prompt_hash: str,
        translated_lines: list[str],
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_cache
                    (source_hash, target_language, llm_type, model, content_type,
                     custom_prompt_hash, translated_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT
                    (source_hash, target_language, llm_type, model, content_type, custom_prompt_hash)
                DO UPDATE SET translated_json = excluded.translated_json,
                              created_at = excluded.created_at
                """,
                (
                    source_hash, target_language, llm_type, model, content_type,
                    custom_prompt_hash, json.dumps(translated_lines, ensure_ascii=False), now,
                ),
            )

    def record_scan_result(self, result: dict[str, Any]) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_status
                    (id, last_scan_at, scanned, queued, skipped, pending_count, throttled, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_scan_at = excluded.last_scan_at,
                    scanned = excluded.scanned,
                    queued = excluded.queued,
                    skipped = excluded.skipped,
                    pending_count = excluded.pending_count,
                    throttled = excluded.throttled,
                    updated_at = excluded.updated_at
                """,
                (
                    now,
                    int(result.get("scanned", 0)),
                    int(result.get("queued", 0)),
                    int(result.get("skipped", 0)),
                    int(result.get("pending_count", 0)),
                    1 if result.get("throttled") else 0,
                    now,
                ),
            )

    def get_scan_status(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT last_scan_at, scanned, queued, skipped, pending_count, throttled
                FROM scan_status WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return {"last_scan_at": None}
        return {
            "last_scan_at": row["last_scan_at"],
            "scanned": row["scanned"],
            "queued": row["queued"],
            "skipped": row["skipped"],
            "pending_count": row["pending_count"],
            "throttled": bool(row["throttled"]),
        }

    def has_active_task(self, path_key: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM tasks
                WHERE file_path_key = ? AND status IN ('pending', 'processing')
                LIMIT 1
                """,
                (path_key,),
            ).fetchone()
        return row is not None

    def bulk_check_active_tasks(self, path_keys: list[str]) -> set[str]:
        """Return the subset of `path_keys` that already have a pending/processing task.

        Replaces the per-file `has_active_task` lookup in the scanner with a
        single IN-clause query. The IN list is hard-capped at 1000 to avoid
        SQLite variable limits; the caller chunks if needed.
        """
        if not path_keys:
            return set()
        out: set[str] = set()
        chunk_size = 1000
        for start in range(0, len(path_keys), chunk_size):
            chunk = path_keys[start:start + chunk_size]
            with self.connect() as connection:
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT file_path_key
                    FROM tasks
                    WHERE file_path_key IN ({placeholders})
                      AND status IN ('pending', 'processing')
                    """,
                    chunk,
                ).fetchall()
            for row in rows:
                out.add(row["file_path_key"])
        return out

    def has_task_for_file_version(self, path_key: str, size_bytes: int, mtime: float) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM tasks
                WHERE file_path_key = ?
                  AND source_size_bytes = ?
                  AND source_mtime = ?
                LIMIT 1
                """,
                (path_key, size_bytes, mtime),
            ).fetchone()
        return row is not None

    def bulk_check_existing_versions(
        self, items: list[tuple[str, int, float]]
    ) -> set[tuple[str, int, float]]:
        """Return the subset of (path_key, size_bytes, mtime) tuples that already
        have at least one task. Replaces per-file `has_task_for_file_version`."""
        if not items:
            return set()
        out: set[tuple[str, int, float]] = set()
        chunk_size = 1000
        for start in range(0, len(items), chunk_size):
            chunk = items[start:start + chunk_size]
            with self.connect() as connection:
                # Build a parameterized IN list of (key, size, mtime) tuples.
                # SQLite supports row-value IN via "IN ((?,?,?), ...)" so we can
                # send a single query rather than 3*N parameters.
                placeholders = ",".join("(?,?,?)" for _ in chunk)
                flat: list[Any] = []
                for key, size, mtime in chunk:
                    flat.extend([key, int(size), float(mtime)])
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT file_path_key, source_size_bytes, source_mtime
                    FROM tasks
                    WHERE (file_path_key, source_size_bytes, source_mtime) IN ({placeholders})
                    """,
                    flat,
                ).fetchall()
            for row in rows:
                out.add(
                    (row["file_path_key"], int(row["source_size_bytes"]), float(row["source_mtime"]))
                )
        return out

    def bulk_create_tasks(
        self,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert many pending tasks in a single transaction.

        Args:
            items: list of {file_id, file_path, size_bytes, mtime, parent_dir_ctime}
                dicts (shape matches create_task's positional args).

        Returns:
            list of created task dicts (the same shape as `create_task`).
        """
        if not items:
            return []
        config = self.get_config()
        max_retries = int(config["processing"]["max_retries"])
        restart_required = 1 if config["meta"].get("restart_required") else 0
        now = utc_now()
        out: list[dict[str, Any]] = []
        chunk_size = 200
        for start in range(0, len(items), chunk_size):
            chunk = items[start:start + chunk_size]
            with self.connect() as connection:
                ids: list[int] = []
                for it in chunk:
                    cursor = connection.execute(
                        """
                        INSERT INTO tasks (
                            file_id, file_path, file_path_key, source_size_bytes, source_mtime,
                            status, stage, progress, retry_count, max_retries,
                            cancel_requested, restart_required, parent_dir_ctime, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, 'pending', 'queued', 0, 0, ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            it["file_id"],
                            it["file_path"],
                            normalize_path(it["file_path"]),
                            int(it["size_bytes"]),
                            float(it["mtime"]),
                            max_retries,
                            restart_required,
                            float(it.get("parent_dir_ctime", 0.0) or 0.0),
                            now,
                            now,
                        ),
                    )
                    ids.append(cursor.lastrowid)
            for it, task_id in zip(chunk, ids):
                out.append(
                    {
                        "id": task_id,
                        "file_id": it["file_id"],
                        "file_path": it["file_path"],
                        "file_path_key": normalize_path(it["file_path"]),
                        "source_size_bytes": int(it["size_bytes"]),
                        "source_mtime": float(it["mtime"]),
                        "status": "pending",
                        "stage": "queued",
                    }
                )
        return out

    def create_task(self, file_id: int, file_path: str, size_bytes: int, mtime: float, parent_dir_ctime: float = 0.0) -> dict[str, Any]:
        config = self.get_config()
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks (
                    file_id, file_path, file_path_key, source_size_bytes, source_mtime,
                    status, stage, progress, retry_count, max_retries,
                    cancel_requested, restart_required, parent_dir_ctime, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', 'queued', 0, 0, ?, 0, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    file_path,
                    normalize_path(file_path),
                    size_bytes,
                    mtime,
                    int(config["processing"]["max_retries"]),
                    1 if config["meta"]["restart_required"] else 0,
                    parent_dir_ctime,
                    now,
                    now,
                ),
            )
            task_id = cursor.lastrowid
        self.log(task_id, "queue", "INFO", "任务已入队", {"file_path": file_path})
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("failed to load created task")
        return task

    def claim_next_pending_task(self) -> dict[str, Any] | None:
        config = self.get_config()
        snapshot = self._build_result_affecting_snapshot(config)
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, file_path, retry_count, max_retries, stage, progress
                FROM tasks
                WHERE status = 'pending'
                ORDER BY parent_dir_ctime DESC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            start_stage = "extract_audio" if row["stage"] == "queued" else normalize_stage_name(str(row["stage"]))
            connection.execute(
                """
                UPDATE tasks
                SET status = 'processing',
                    stage = ?,
                    progress = ?,
                    config_snapshot = ?,
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE id = ?
                """,
                (
                    start_stage,
                    0 if row["stage"] == "queued" else float(row["progress"]),
                    json.dumps(snapshot),
                    now,
                    now,
                    row["id"],
                ),
            )
            task_id = row["id"]
        self.log(task_id, "processing", "INFO", "任务开始处理", {"config_snapshot": snapshot})
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("failed to claim pending task")
        return task

    def update_task_stage(self, task_id: int, stage: str, progress: float, status: str = "processing") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET stage = ?, progress = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (stage, progress, status, utc_now(), task_id),
            )

    def mark_task_done(self, task_id: int, result_payload: dict[str, Any]) -> None:
        task = self.get_task(task_id)
        final_stage = "mux" if task and task["config_snapshot"] and task["config_snapshot"]["mux"]["enabled"] else "output_finalize"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'done',
                    stage = ?,
                    progress = 100,
                    result_payload = ?,
                    error_message = NULL,
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (final_stage, json.dumps(result_payload), now, now, task_id),
            )
        self.log(task_id, final_stage, "INFO", "任务处理完成", result_payload)

    def mark_task_cancelled(self, task_id: int, stage: str, message: str = "任务已取消") -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled',
                    stage = ?,
                    progress = progress,
                    error_message = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (stage, message, now, now, task_id),
            )
        self.log(task_id, stage, "WARNING", message)

    def mark_task_failure(self, task_id: int, stage: str, message: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError("task not found during failure")
        retry_count = int(task["retry_count"])
        max_retries = int(task["max_retries"])
        should_retry = retry_count < max_retries
        retry_mode = "restart"
        details: dict[str, Any] = {"will_retry": should_retry}
        if should_retry and task["config_snapshot"]:
            retry_mode = str(task["config_snapshot"]["processing"].get("retry_mode", "restart"))
            if retry_mode == "resume":
                feasibility = check_resume_feasibility(task)
                if not feasibility["can_resume"]:
                    retry_mode = "restart"
                    details["resume_missing"] = feasibility["missing"]
                else:
                    stage = str(feasibility.get("resume_stage", normalize_stage_name(stage)))
        if should_retry and retry_mode == "restart":
            cleanup_intermediates(Path(task["file_path"]))
            cleanup_work_dir_intermediates(self._resolve_task_work_dir(task_id, task["config_snapshot"]))
        now = utc_now()
        with self.connect() as connection:
            if should_retry:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending',
                        stage = ?,
                        progress = ?,
                        retry_count = retry_count + 1,
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "queued" if retry_mode == "restart" else stage,
                        0 if retry_mode == "restart" else float(task["progress"]),
                        message,
                        now,
                        task_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed',
                        stage = ?,
                        error_message = ?,
                        updated_at = ?,
                        finished_at = ?
                    WHERE id = ?
                    """,
                    (stage, message, now, now, task_id),
                )
        level = "WARNING" if should_retry else "ERROR"
        details["retry_mode"] = retry_mode
        self.log(task_id, stage, level, message, details)
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("failed to reload task after failure")
        return updated

    def delete_task(self, task_id: int) -> bool:
        with self.connect() as connection:
            connection.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    def is_cancel_requested(self, task_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    # ---- Quality / suspect task lookup ----

    def get_suspect_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return done tasks whose `result_payload.quality_report.is_suspect` is true.

        Used by the dashboard "可疑字幕" card. The lookup uses SQLite's
        json_extract to avoid loading every done task into memory; tasks
        without a quality_report (older runs) are silently skipped.
        """
        capped = max(1, min(int(limit), 200))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, file_path, status, stage, progress, error_message,
                       result_payload, finished_at
                FROM tasks
                WHERE status = 'done'
                  AND result_payload IS NOT NULL
                  AND json_extract(result_payload, '$.quality_report.is_suspect') = 1
                ORDER BY finished_at DESC, id DESC
                LIMIT ?
                """,
                (capped,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["result_payload"]) if row["result_payload"] else {}
            quality = payload.get("quality_report") or {}
            items.append(
                {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "status": row["status"],
                    "stage": normalize_stage_name(str(row["stage"])),
                    "progress": row["progress"],
                    "error_message": row["error_message"],
                    "finished_at": row["finished_at"],
                    "quality_report": quality,
                }
            )
        return items

    # ---- Dashboard statistics ----

    def get_dashboard_stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
            total = sum(r["count"] for r in status_rows)
            status_map = {r["status"]: r["count"] for r in status_rows}
            done = status_map.get("done", 0)
            failed = status_map.get("failed", 0)
            cancelled = status_map.get("cancelled", 0)
            processing = status_map.get("processing", 0)
            pending = status_map.get("pending", 0)
            success_rate = round(done / total * 100, 1) if total > 0 else 0.0

            # Average processing duration for completed tasks
            duration_row = connection.execute(
                """
                SELECT AVG(
                    (julianday(COALESCE(finished_at, updated_at)) - julianday(started_at)) * 86400
                ) AS avg_seconds
                FROM tasks
                WHERE status = 'done' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                """
            ).fetchone()
            avg_duration = round(duration_row["avg_seconds"]) if duration_row and duration_row["avg_seconds"] else 0

            # Daily trend (last 14 days)
            trend_rows = connection.execute(
                """
                SELECT DATE(finished_at) AS day, COUNT(*) AS count
                FROM tasks
                WHERE status = 'done' AND finished_at IS NOT NULL
                  AND finished_at >= datetime('now', '-14 days')
                GROUP BY DATE(finished_at)
                ORDER BY day DESC
                """
            ).fetchall()
            daily_trend = [{"date": r["day"], "count": r["count"]} for r in trend_rows]

            # Average processing duration by stage (for bottleneck analysis)
            stage_rows = connection.execute(
                """
                SELECT stage, COUNT(*) AS count,
                       AVG(
                           (julianday(COALESCE(finished_at, updated_at)) - julianday(started_at)) * 86400
                       ) AS avg_seconds
                FROM tasks
                WHERE status = 'done' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                GROUP BY stage
                ORDER BY avg_seconds DESC
                LIMIT 8
                """
            ).fetchall()
            stage_stats = [
                {"stage": normalize_stage_name(r["stage"]), "count": r["count"], "avg_seconds": round(r["avg_seconds"]) if r["avg_seconds"] else 0}
                for r in stage_rows
            ]

        return {
            "total": total,
            "done": done,
            "failed": failed,
            "cancelled": cancelled,
            "processing": processing,
            "pending": pending,
            "success_rate": success_rate,
            "avg_duration_seconds": avg_duration,
            "daily_trend": daily_trend,
            "stage_stats": stage_stats,
        }

    # ---- Task search ----

    def list_tasks(
        self,
        page: int,
        page_size: int,
        status: str | None = None,
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> PageResult:
        offset = max(page - 1, 0) * page_size
        conditions: list[str] = []
        params: list[Any] = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if search:
            conditions.append("file_path LIKE ?")
            params.append(f"%{search}%")
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        if status in ("done", "failed"):
            order_clause = "ORDER BY updated_at DESC, id DESC"
        else:
            order_clause = "ORDER BY parent_dir_ctime DESC, id DESC"

        with self.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM tasks {where_clause}",
                params,
            ).fetchone()[0]
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks
                GROUP BY status
                """
            ).fetchall()
            rows = connection.execute(
                f"""
                SELECT id, file_path, status, stage, progress, retry_count, max_retries, cancel_requested,
                       restart_required, error_message, created_at, updated_at, started_at, finished_at
                FROM tasks
                {where_clause}
                {order_clause}
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return PageResult(
            [
                {
                    **dict(row),
                    "stage": normalize_stage_name(str(row["stage"])),
                }
                for row in rows
            ],
            page,
            page_size,
            total,
            {str(row["status"]): int(row["count"]) for row in status_rows},
        )

    # ---- Batch operations ----

    def batch_cancel(self, task_ids: list[int]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" * len(task_ids))
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks SET cancel_requested = 1, updated_at = ?
                WHERE id IN ({placeholders}) AND status = 'processing'
                """,
                [now, *task_ids],
            )
        return cursor.rowcount

    def batch_delete(self, task_ids: list[int]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" * len(task_ids))
        with self.connect() as connection:
            connection.execute(
                f"DELETE FROM task_logs WHERE task_id IN ({placeholders})",
                task_ids,
            )
            cursor = connection.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",
                task_ids,
            )
        return cursor.rowcount

    def batch_retry(self, task_ids: list[int]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for task_id in task_ids:
            task = self.get_task(task_id)
            if task and task["status"] in {"failed", "cancelled", "done"}:
                cleanup_intermediates(Path(task["file_path"]))
                cleanup_work_dir_intermediates(self._resolve_task_work_dir(task_id, task["config_snapshot"]))
                with self.connect() as connection:
                    connection.execute(
                        """
                        UPDATE tasks
                        SET status = 'pending', stage = 'queued', progress = 0,
                            cancel_requested = 0, error_message = NULL, result_payload = NULL,
                            updated_at = ?, started_at = NULL, finished_at = NULL
                        WHERE id = ? AND status IN ('failed', 'cancelled', 'done')
                        """,
                        (utc_now(), task_id),
                    )
                results.append({"id": task_id, "status": "pending"})
            else:
                results.append({"id": task_id, "status": "skipped"})
        return results
