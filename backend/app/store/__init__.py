"""Public surface for the SubtitlePipeline SQLite store.

Split from a single 1260-LOC module into focused submodules:
- ``app.store`` (this file) — ``Database`` facade: connections, task
  lifecycle/query, file observation, scan status. Delegates schema bootstrap
  to ``DatabaseMigrations`` and config CRUD to ``ConfigService``.
- ``app.store.migrations`` — schema bootstrap + ``_ensure_column``.
- ``app.store.config`` — config CRUD + in-process cache + version tracking.

Backward compatibility is load-bearing: ``from app.store import Database``
and every pre-refactor ``Database.xxx()`` method continues to work.
Cache attrs (``_config_cache``, ``_cache_valid``, ``_cached_config_version``)
are property proxies over ``ConfigService`` state so tests that mutate
``database._cached_config_version`` directly still pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

from ..defaults import RESULT_AFFECTING_GROUPS
from ..events import emit, task_to_event_payload
from ..pipeline import (
    check_resume_feasibility,
    cleanup_intermediates,
    cleanup_work_dir_intermediates,
    normalize_stage_name,
)
from .config import (
    READ_ONLY_CONFIG_FIELDS,
    ConfigService,
    _migrate_whisper_config_dict,
    _normalize_align_provider,
    _normalize_legacy_align_method,
)
from .migrations import DatabaseMigrations, OBSOLETE_CONFIG_FIELDS


def _fire_event(event_type: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget event emission; no-op when no event loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(emit(event_type, payload))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_path(value: str) -> str:
    return str(Path(value).expanduser().resolve()).lower()


@dataclass
class PageResult:
    items: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    status_counts: dict[str, int]


class Database:
    """Facade over the SubtitlePipeline SQLite store.

    Owns connection management + task/file/scan surface area; delegates schema
    bootstrap to :class:`DatabaseMigrations` and config CRUD to
    :class:`ConfigService`. Public API unchanged from the pre-refactor monolith.
    """

    def __init__(self, db_path: str, persistent: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.persistent = persistent
        self._conn = self._create_connection() if persistent else None
        self.migrations = DatabaseMigrations(self)
        self.config_service = ConfigService(self)

    # -- Connection management (shared with extracted services) --

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

    # -- Schema / migration delegation --

    def initialize(self) -> None:
        return self.migrations.initialize()

    def _ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        definition: str,
    ) -> None:
        return self.migrations._ensure_column(connection, table_name, column_name, definition)

    # -- Config CRUD delegation --
    # Cache state lives on ConfigService; the property proxies below keep the
    # pre-refactor test contract intact (tests mutate
    # ``database._cached_config_version`` to simulate version drift).

    def get_config(self) -> dict[str, Any]:
        return self.config_service.get_config()

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.config_service.update_config(payload)

    def _get_config_uncached(self) -> dict[str, Any]:
        return self.config_service._get_config_uncached()

    def _get_config_version(self) -> int:
        return self.config_service._get_config_version()

    def _bump_config_version(self, connection: sqlite3.Connection) -> int:
        return self.config_service._bump_config_version(connection)

    @property
    def _config_cache(self) -> dict[str, Any] | None:
        return self.config_service._config_cache

    @_config_cache.setter
    def _config_cache(self, value: dict[str, Any] | None) -> None:
        self.config_service._config_cache = value

    @property
    def _cache_valid(self) -> bool:
        return self.config_service._cache_valid

    @_cache_valid.setter
    def _cache_valid(self, value: bool) -> None:
        self.config_service._cache_valid = value

    @property
    def _cached_config_version(self) -> int | None:
        return self.config_service._cached_config_version

    @_cached_config_version.setter
    def _cached_config_version(self, value: int | None) -> None:
        self.config_service._cached_config_version = value

    # -- Config-adjacent helpers --

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

    # ------------------------------------------------------------------
    # Task query surface
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Task lifecycle
    # ------------------------------------------------------------------

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
        task = self.get_task(task_id)
        if task is not None:
            _fire_event("task.updated", task_to_event_payload(task))
        return task

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
        task = self.get_task(task_id)
        if task is not None:
            _fire_event("task.updated", task_to_event_payload(task))
        return task

    # ------------------------------------------------------------------
    # File observation
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Translation cache
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Scan status
    # ------------------------------------------------------------------

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

    # -- Batch helpers (used by ScannerService.scan_once to eliminate N+1 queries) --

    _SQL_PARAM_CHUNK = 500

    @staticmethod
    def _chunk(seq: list[str], size: int) -> Iterator[list[str]]:
        for i in range(0, len(seq), size):
            yield seq[i : i + size]

    def observe_files_batch(self, files: list[tuple[str, int, float]]) -> list[dict[str, Any]]:
        """Batch version of observe_file — observes all files in a single transaction.

        Returns a list of observed-file dicts in the same order as the input.
        """
        if not files:
            return []
        now = utc_now()
        entries: list[dict[str, Any]] = []
        for file_path, size_bytes, mtime in files:
            path_key = normalize_path(file_path)
            entries.append(
                {
                    "path": file_path,
                    "path_key": path_key,
                    "size_bytes": int(size_bytes),
                    "mtime": float(mtime),
                }
            )
        results: list[dict[str, Any]] = []
        with self.connect() as connection:
            existing_map: dict[str, Any] = {}
            for chunk in self._chunk([e["path_key"] for e in entries], self._SQL_PARAM_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"""
                    SELECT id, path_key, size_bytes, mtime, stable_hits
                    FROM files
                    WHERE path_key IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    existing_map[row["path_key"]] = row
            for entry in entries:
                existing = existing_map.get(entry["path_key"])
                if existing:
                    matches = (
                        existing["size_bytes"] == entry["size_bytes"]
                        and float(existing["mtime"]) == float(entry["mtime"])
                    )
                    stable_hits = int(existing["stable_hits"]) + 1 if matches else 1
                    connection.execute(
                        """
                        UPDATE files
                        SET path = ?, size_bytes = ?, mtime = ?, stable_hits = ?, last_seen_at = ?
                        WHERE path_key = ?
                        """,
                        (entry["path"], entry["size_bytes"], entry["mtime"], stable_hits, now, entry["path_key"]),
                    )
                    file_id = existing["id"]
                else:
                    stable_hits = 1
                    cursor = connection.execute(
                        """
                        INSERT INTO files (path, path_key, size_bytes, mtime, stable_hits, first_seen_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (entry["path"], entry["path_key"], entry["size_bytes"], entry["mtime"], stable_hits, now, now),
                    )
                    file_id = cursor.lastrowid
                results.append(
                    {
                        "file_id": file_id,
                        "path": entry["path"],
                        "path_key": entry["path_key"],
                        "size_bytes": entry["size_bytes"],
                        "mtime": entry["mtime"],
                        "stable_hits": stable_hits,
                    }
                )
        return results

    def get_active_task_path_keys(self, path_keys: list[str]) -> set[str]:
        """Batch version of has_active_task — returns the set of path_keys that have active tasks."""
        if not path_keys:
            return set()
        result: set[str] = set()
        with self.connect() as connection:
            for chunk in self._chunk(path_keys, self._SQL_PARAM_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT file_path_key
                    FROM tasks
                    WHERE file_path_key IN ({placeholders})
                      AND status IN ('pending', 'processing')
                    """,
                    chunk,
                ).fetchall()
                result.update(row["file_path_key"] for row in rows)
        return result

    def get_existing_task_versions(self, path_keys: list[str]) -> set[tuple[str, int, float]]:
        """Batch version of has_task_for_file_version — returns (path_key, size, mtime) tuples."""
        if not path_keys:
            return set()
        result: set[tuple[str, int, float]] = set()
        with self.connect() as connection:
            for chunk in self._chunk(path_keys, self._SQL_PARAM_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT file_path_key, source_size_bytes, source_mtime
                    FROM tasks
                    WHERE file_path_key IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    result.add((row["file_path_key"], int(row["source_size_bytes"]), float(row["source_mtime"])))
        return result

    # ------------------------------------------------------------------
    # Task lifecycle: creation, claiming, terminal transitions
    # ------------------------------------------------------------------

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
        _fire_event("task.updated", task_to_event_payload(task))
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
        _fire_event("task.updated", task_to_event_payload(task))
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
        updated = self.get_task(task_id)
        if updated is not None:
            _fire_event("task.updated", task_to_event_payload(updated))

    def mark_task_cancelled(self, task_id: int, stage: str, message: str = "任务已取消") -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET status = 'cancelled',
                    stage = ?,
                    progress = 0,
                    error_message = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (stage, message, now, now, task_id),
            )
        self.log(task_id, stage, "WARNING", message)
        updated = self.get_task(task_id)
        if updated is not None:
            _fire_event("task.updated", task_to_event_payload(updated))

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
        _fire_event("task.updated", task_to_event_payload(updated))
        return updated

    def delete_task(self, task_id: int) -> bool:
        with self.connect() as connection:
            connection.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            _fire_event("task.deleted", {"id": task_id})
        return deleted

    def is_cancel_requested(self, task_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])


__all__ = [
    # Facade + data classes
    "Database",
    "PageResult",
    # Extracted services
    "ConfigService",
    "DatabaseMigrations",
    # Constants
    "OBSOLETE_CONFIG_FIELDS",
    "READ_ONLY_CONFIG_FIELDS",
    # Utilities
    "utc_now",
    "normalize_path",
    "_fire_event",
    "_migrate_whisper_config_dict",
    "_normalize_align_provider",
    "_normalize_legacy_align_method",
]
