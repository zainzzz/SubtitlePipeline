from __future__ import annotations

import fnmatch
import logging
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .pipeline import (
    CancellationRequested,
    PipelineError,
    TaskContext,
    align_segments,
    build_result_payload,
    cleanup_intermediates,
    cleanup_work_dir_intermediates,
    ensure_intermediates_dir,
    expected_target_subtitle_paths,
    extract_audio,
    load_asr_result,
    load_aligned_segments,
    load_processed_segments,
    load_translations,
    mux_subtitle,
    normalize_stage_name,
    process_text_segments,
    read_stage_artifacts,
    render_srt,
    resolve_audio_path,
    run_asr,
    save_aligned_segments,
    save_asr_result,
    save_processed_segments,
    save_translations,
    translate_segments,
    write_stage_artifacts,
    WhisperModelCache,
)
from .quality_checker import check_quality
from .store import Database, normalize_path
from .system_monitor import check_resources as check_asr_resources
from .event_bus import emit


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".m4v"}
logger = logging.getLogger(__name__)


def _mux_output_suffix(config: dict) -> str:
    """Return the fixed suffix of mux output filenames (e.g. '.subbed.mkv')."""
    template = str(config.get("mux", {}).get("filename_template", "")).strip()
    if not template:
        return ""
    suffix = template.replace("{stem}", "")
    return suffix if suffix else ""


def _is_inside_directory(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        return False


def _should_skip_scan_path(path: Path, config: dict[str, Any]) -> bool:
    if ".subpipeline" in path.parts:
        return True
    exclude_dirs = config.get("file", {}).get("exclude_dirs") or []
    if exclude_dirs and any(
        fnmatch.fnmatch(part, pattern) for part in path.parts for pattern in exclude_dirs
    ):
        return True
    if not bool(config.get("file", {}).get("output_to_source_dir", True)):
        output_dir = Path(os.environ.get("SUBPIPELINE_OUTPUT_DIR", "/output"))
        if _is_inside_directory(path, output_dir):
            return True
    mux_suffix = _mux_output_suffix(config)
    return bool(mux_suffix and path.name.endswith(mux_suffix))


def _has_existing_target_subtitle(path: Path, config: dict[str, Any]) -> bool:
    # 按当前字幕命名规则,检查是否已有目标语言字幕;命中则视为无需再处理
    expected = expected_target_subtitle_paths(
        path,
        config.get("file", {}),
        config.get("subtitle", {}),
        config.get("translation", {}),
    )
    return any(p.exists() for p in expected)


@dataclass
class ScanResult:
    scanned: int
    queued: int
    skipped: int
    pending_count: int
    remaining_slots: int
    throttled: bool


class ScannerService:
    def __init__(self, database: Database):
        self.database = database

    def scan_once(self) -> ScanResult:
        config = self.database.get_config()
        file_config = config["file"]
        roots = [Path(p) for p in (file_config.get("input_dirs") or [])] or [Path(file_config["input_dir"])]
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
        allowed = {extension.lower() for extension in file_config.get("allowed_extensions", [])} or VIDEO_EXTENSIONS
        min_size_bytes = int(file_config["min_size_mb"]) * 1024 * 1024
        max_size_bytes = int(file_config["max_size_mb"]) * 1024 * 1024
        max_pending_tasks = int(file_config.get("max_pending_tasks", 100))
        pending_count = self.database.count_tasks_by_status("pending")
        if pending_count >= max_pending_tasks:
            logger.info(
                "backpressure: pending=%d >= max_pending_tasks=%d，本轮跳过扫描",
                pending_count, max_pending_tasks,
            )
            self.database.record_scan_result({
                "scanned": 0, "queued": 0, "skipped": 0,
                "pending_count": pending_count, "throttled": True,
            })
            return ScanResult(
                scanned=0, queued=0, skipped=0,
                pending_count=pending_count, remaining_slots=0, throttled=True,
            )

        @lru_cache(maxsize=None)
        def _dir_ctime(d: str) -> float:
            try:
                return os.stat(d).st_ctime
            except OSError:
                return 0.0

        # 1. Walk the directory tree and collect candidate files (cheap, IO-bound).
        #    Stage the path / size / mtime in memory so the rest of the scan runs
        #    against a snapshot and not against the live filesystem.
        all_files: list[Path] = []
        root_of: dict[str, Path] = {}
        for root in roots:
            for p in root.rglob("*"):
                if p.is_file():
                    all_files.append(p)
                    root_of[str(p)] = root

        def _sort_key(p: Path):
            root = root_of.get(str(p), roots[0])
            depth = len(p.parts) - len(root.parts) - 1
            parent_ctime = _dir_ctime(str(p.parent))
            return (depth, -parent_ctime, p.name)

        all_files.sort(key=_sort_key)

        # 2. Cheap in-memory filters: skip paths / extension / size range.
        #    Skip stat() on the path itself — we just read st_size/st_mtime
        #    via os.stat, same as the previous per-file path.stat() call.
        candidates: list[tuple[Path, int, float]] = []
        scanned = 0
        for path in all_files:
            if _should_skip_scan_path(path, config):
                continue
            if path.suffix.lower() not in allowed:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            size = int(stat.st_size)
            if size < min_size_bytes or size > max_size_bytes:
                scanned += 1
                continue
            candidates.append((path, size, float(stat.st_mtime)))
            scanned += 1

        if not candidates:
            self.database.record_scan_result({
                "scanned": scanned, "queued": 0, "skipped": 0,
                "pending_count": pending_count, "throttled": False,
            })
            return ScanResult(
                scanned=scanned, queued=0, skipped=0,
                pending_count=pending_count, remaining_slots=0, throttled=False,
            )

        # 3. Batch: upsert all candidates into files table in one transaction
        #    (was: 1 SQL per file → now 1 SQL for the whole batch).
        observe_inputs = [
            {"file_path": str(p), "size_bytes": size, "mtime": mtime}
            for p, size, mtime in candidates
        ]
        observes = self.database.bulk_observe_files(observe_inputs)

        # 4. Apply stable_hits and subtitle-already-exists filters in memory.
        #    Files that haven't been seen twice yet are deferred to the next
        #    scan rather than queued. Files that already have a final .srt
        #    on disk are skipped (this one is unavoidably a per-file FS call).
        to_create: list[dict[str, Any]] = []
        skipped_stable = 0
        skipped_subtitle = 0
        for (path, size, mtime), observe in zip(candidates, observe_inputs):
            observed = observes[normalize_path(str(path))]
            if observed["stable_hits"] < 2:
                skipped_stable += 1
                continue
            if _has_existing_target_subtitle(path, config):
                skipped_subtitle += 1
                continue
            to_create.append(
                {
                    "file_id": observed["file_id"],
                    "file_path": observed["path"],
                    "size_bytes": observed["size_bytes"],
                    "mtime": observed["mtime"],
                    "parent_dir_ctime": _dir_ctime(str(path.parent)),
                }
            )

        # 5. Batch: detect already-handled versions + active tasks in 2 SQLs
        #    (was: 2 SQLs per file).
        version_keys = [
            (normalize_path(it["file_path"]), it["size_bytes"], it["mtime"])
            for it in to_create
        ]
        existing_versions = self.database.bulk_check_existing_versions(version_keys)
        active_keys = self.database.bulk_check_active_tasks([k for (k, _, _) in version_keys])
        to_create_filtered = [
            it for it in to_create
            if (normalize_path(it["file_path"]), it["size_bytes"], it["mtime"]) not in existing_versions
            and normalize_path(it["file_path"]) not in active_keys
        ]
        skipped_existing = len(to_create) - len(to_create_filtered)

        # 6. Apply throttle: respect max_pending_tasks. Trim to remaining slots.
        remaining_slots = max(0, max_pending_tasks - pending_count)
        throttled = False
        if len(to_create_filtered) > remaining_slots:
            to_create_filtered = to_create_filtered[:remaining_slots]
            throttled = True

        # 7. Batch: insert all queued tasks in a single transaction.
        self.database.bulk_create_tasks(to_create_filtered)

        queued = len(to_create_filtered)
        skipped = skipped_stable + skipped_subtitle + skipped_existing
        self.database.record_scan_result({
            "scanned": scanned, "queued": queued, "skipped": skipped,
            "pending_count": pending_count, "throttled": throttled,
        })
        return ScanResult(
            scanned=scanned,
            queued=queued,
            skipped=skipped,
            pending_count=pending_count,
            remaining_slots=0,
            throttled=throttled,
        )

    def run_forever(self) -> None:
        while True:
            file_config = self.database.get_config()["file"]
            interval = max(int(file_config["scan_interval_seconds"]), 1)
            if not self.database.is_setup_complete():
                time.sleep(interval)
                continue
            if not file_config.get("scan_enabled", True):
                logger.info("扫描已暂停 (scan_enabled=False)，等待恢复")
                time.sleep(interval)
                continue
            result = self.scan_once()
            logger.info(
                "扫描完成 scanned=%d queued=%d skipped=%d pending=%d slots=%d throttled=%s",
                result.scanned,
                result.queued,
                result.skipped,
                result.pending_count,
                result.remaining_slots,
                result.throttled,
            )
            interval = int(self.database.get_config()["file"]["scan_interval_seconds"])
            time.sleep(max(interval, 1))


class WorkerService:
    def __init__(self, database: Database):
        self.database = database
        self.model_cache = WhisperModelCache()

    def run_forever(self) -> None:
        while True:
            if not self.database.is_setup_complete():
                interval = int(self.database.get_config()["processing"]["poll_interval_seconds"])
                time.sleep(max(interval, 1))
                continue
            if not self._is_within_schedule_window():
                interval = int(self.database.get_config()["processing"]["poll_interval_seconds"])
                time.sleep(max(interval, 1))
                continue
            processed = self.process_next_task()
            if not processed:
                interval = int(self.database.get_config()["processing"]["poll_interval_seconds"])
                time.sleep(max(interval, 1))

    def _is_within_schedule_window(self) -> bool:
        config = self.database.get_config()
        schedule_cfg = config.get("schedule", {})
        if not schedule_cfg.get("enabled"):
            return True
        from datetime import datetime, time as dt_time
        import zoneinfo
        tz_name = str(schedule_cfg.get("timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            tz = zoneinfo.ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        start_parts = str(schedule_cfg.get("start_time", "00:00")).split(":")
        end_parts = str(schedule_cfg.get("end_time", "23:59")).split(":")
        try:
            start_t = dt_time(int(start_parts[0]), int(start_parts[1]))
            end_t = dt_time(int(end_parts[0]), int(end_parts[1]))
        except (ValueError, IndexError):
            return True
        if start_t <= end_t:
            return start_t <= now.time() <= end_t
        return now.time() >= start_t or now.time() <= end_t

    def process_next_task(self) -> bool:
        task = self.database.claim_next_pending_task()
        if not task:
            return False
        emit("task.started", {"task_id": task["id"], "file_path": task["file_path"], "stage": task["stage"]})
        try:
            result_payload = self._process_claimed_task(task)
            self.database.mark_task_done(task["id"], result_payload)
            emit("task.done", {"task_id": task["id"], "file_path": task["file_path"]})
            self._send_webhook(task)
        except CancellationRequested:
            latest = self.database.get_task(task["id"])
            self.database.mark_task_cancelled(task["id"], latest["stage"] if latest else task["stage"])
            emit("task.cancelled", {"task_id": task["id"]})
        except Exception as exc:
            latest = self.database.get_task(task["id"])
            self.database.mark_task_failure(task["id"], latest["stage"] if latest else task["stage"], str(exc))
            emit("task.failed", {"task_id": task["id"], "error": str(exc)})
        return True

    def _send_webhook(self, task: dict[str, Any]) -> None:
        try:
            from .webhook import send_webhook_notification
            config = self.database.get_config()
            send_webhook_notification(config, task)
        except Exception:
            logger.debug("webhook notification failed", exc_info=True)

    def _check_pre_asr_resources(self, task: dict[str, Any], snapshot: dict[str, Any], audio_path: Path) -> None:
        """Refuse to launch ASR if GPU/disk can't handle the task.

        Skipped silently when pre_asr_resource_check is disabled. On
        insufficient resources, logs the full recommendation list to the
        task log and raises PipelineError — the caller (process_next_task)
        marks the task failed with a user-readable message.
        """
        config = self.database.get_config()
        proc_cfg = (config.get("processing") or {}) if isinstance(config, dict) else {}
        if not proc_cfg.get("pre_asr_resource_check", True):
            return
        try:
            audio_size = audio_path.stat().st_size
        except OSError as exc:
            logger.warning("pre_asr: cannot stat audio %s: %s", audio_path, exc)
            return
        work_dir = Path(snapshot["processing"]["work_dir"])
        model_name = str(snapshot.get("whisper", {}).get("model_name", "") or "")
        check = check_asr_resources(model_name, audio_size, work_dir, config=config)
        if check.ok:
            logger.info(
                "pre_asr resource check passed for task %s (gpu=%s MB free, disk=%s MB free)",
                task["id"], check.gpu_available_mb, check.disk_available_mb,
            )
            return
        # Compose a human-readable message. The PipelineError text becomes
        # the task's error_message; the structured payload is in the log
        # for debugging.
        reasons = [r for r in (check.gpu_reason, check.disk_reason) if r]
        recommendations = "\n".join(f"  - {r}" for r in check.recommendations) or "  (no specific suggestions)"
        message = "ASR 前置资源检查未通过：\n" + "\n".join(reasons) + "\n建议：\n" + recommendations
        logger.warning("pre_asr resource check failed for task %s: %s", task["id"], message)
        self.database.log(task["id"], "run_asr", "ERROR", message, check.to_dict())
        raise PipelineError(message)

    def _process_claimed_task(self, task: dict[str, Any]) -> dict[str, Any]:
        snapshot = task["config_snapshot"]
        if snapshot is None:
            raise PipelineError("缺少配置快照")
        work_dir = Path(snapshot["processing"]["work_dir"]) / str(task["id"])
        work_dir.mkdir(parents=True, exist_ok=True)
        context = TaskContext(
            task_id=task["id"],
            file_path=task["file_path"],
            config_snapshot=snapshot,
            work_dir=work_dir,
        )
        intermediates_dir = ensure_intermediates_dir(context)
        if context.using_fallback_intermediates:
            self.database.log(
                task["id"],
                task["stage"],
                "WARNING",
                "源文件目录不可写，已回退到工作目录保存中间产物",
                {"intermediates_dir": str(intermediates_dir)},
            )
        start_stage = normalize_stage_name(str(task["stage"]))
        audio_path: Path | None = None
        asr_result: dict[str, Any] | None = None
        aligned_segments: list[dict[str, Any]] | None = None
        processed_segments: list[dict[str, Any]] | None = None
        translations: dict[str, list[str]] | None = None
        subtitle_paths: list[str] | None = None
        result_payload: dict[str, Any] | None = None
        if start_stage not in {"queued", "extract_audio"}:
            audio_path = resolve_audio_path(context)
        if start_stage in {"align_segments", "text_process", "translate", "subtitle_render", "output_finalize", "mux"}:
            asr_result = load_asr_result(context)
        if start_stage in {"text_process", "translate", "subtitle_render", "output_finalize", "mux"}:
            aligned_segments = load_aligned_segments(context)
        if start_stage in {"translate", "subtitle_render", "output_finalize", "mux"}:
            processed_segments = load_processed_segments(context)
        if snapshot["translation"]["enabled"] and start_stage in {"subtitle_render", "output_finalize", "mux"}:
            translations = load_translations(context)
        if start_stage in {"output_finalize", "mux"}:
            if processed_segments is None:
                raise PipelineError("缺少字幕渲染依赖，无法继续执行")
            subtitle_paths = render_srt(context, processed_segments, translations or {})
        if start_stage == "mux":
            result_payload = read_stage_artifacts(context)
        if start_stage in {"queued", "extract_audio"}:
            audio_path = self._run_stage(task["id"], "extract_audio", 10, lambda: extract_audio(context))
        if start_stage in {"queued", "extract_audio", "run_asr"}:
            if audio_path is None:
                raise PipelineError("缺少音频文件，无法执行 ASR")
            # Pre-flight: refuse to launch ASR if the GPU doesn't have
            # enough VRAM or work_dir is about to fill the disk. This
            # protects self-hosted NAS boxes from a runaway model load.
            self._check_pre_asr_resources(task, snapshot, audio_path)
            asr_result = self._run_stage(task["id"], "run_asr", 35, lambda: run_asr(context, audio_path, self.model_cache, self.database))
            save_asr_result(context, asr_result)
        if start_stage in {"queued", "extract_audio", "run_asr", "align_segments"}:
            if asr_result is None or audio_path is None:
                raise PipelineError("缺少 ASR 结果，无法执行时间轴对齐")
            aligned_segments = self._run_stage(
                task["id"],
                "align_segments",
                45,
                lambda: align_segments(context, asr_result, audio_path, self.model_cache, self.database),
            )
            save_aligned_segments(context, aligned_segments)
        if start_stage in {"queued", "extract_audio", "run_asr", "align_segments", "text_process"}:
            if aligned_segments is None:
                raise PipelineError("缺少对齐结果，无法继续执行文本处理")
            processed_segments = self._run_stage(
                task["id"],
                "text_process",
                55,
                lambda: process_text_segments(aligned_segments),
            )
            save_processed_segments(context, processed_segments)
        if start_stage in {"queued", "extract_audio", "run_asr", "align_segments", "text_process", "translate"}:
            if processed_segments is None:
                raise PipelineError("缺少分段结果，无法继续执行")
            translations = self._run_stage(
                task["id"],
                "translate",
                60,
                lambda: translate_segments(
                    context,
                    processed_segments,
                    progress_callback=lambda current, total: self.database.update_task_stage(
                        task["id"],
                        "translate",
                        60 + int(20 * current / total),
                    ),
                    database=self.database,
                ),
            )
            save_translations(context, translations)
        if start_stage in {"queued", "extract_audio", "run_asr", "align_segments", "text_process", "translate", "subtitle_render"}:
            if processed_segments is None:
                raise PipelineError("缺少字幕渲染输入，无法继续执行")
            subtitle_paths = self._run_stage(
                task["id"],
                "subtitle_render",
                95,
                lambda: render_srt(context, processed_segments, translations or {}),
            )
        if audio_path is None or subtitle_paths is None:
            raise PipelineError("缺少最终输出所需产物")
        if start_stage in {"queued", "extract_audio", "run_asr", "align_segments", "text_process", "translate", "subtitle_render", "output_finalize"}:
            result_payload = build_result_payload(context, audio_path, subtitle_paths, translations or {})
            # Run the lightweight post-pipeline quality check so the UI can
            # surface a "可疑字幕" list. Cheap (pure-Python, no IO), safe to
            # call on every task. Result is stored inside result_payload and
            # also logged as a WARNING if suspect.
            quality_cfg = snapshot.get("quality", {}) or {}
            if quality_cfg.get("enabled", True):
                asr_segments = None
                if isinstance(asr_result, dict):
                    asr_segments = asr_result.get("segments")
                quality = check_quality(
                    processed_segments or [],
                    translations or {},
                    asr_segments=asr_segments if isinstance(asr_segments, list) else None,
                    config=snapshot,
                )
                result_payload["quality_report"] = quality.to_dict()
                if quality.is_suspect:
                    self.database.log(
                        task["id"],
                        "output_finalize",
                        "WARNING",
                        f"字幕质量自检命中: {quality.summary}",
                        {"score": quality.score, "issue_codes": [i.code for i in quality.issues]},
                    )
            self._run_stage(task["id"], "output_finalize", 100, lambda: write_stage_artifacts(context, result_payload))
        if result_payload is None:
            raise PipelineError("缺少任务结果元数据")
        if snapshot["mux"]["enabled"]:
            mux_path = self._run_stage(task["id"], "mux", 100, lambda: mux_subtitle(context, subtitle_paths))
            result_payload["mux_path"] = mux_path
            write_stage_artifacts(context, result_payload)
        if not snapshot["processing"]["keep_intermediates"]:
            cleanup_intermediates(Path(task["file_path"]))
            if context.using_fallback_intermediates:
                cleanup_work_dir_intermediates(context.work_dir)
        return result_payload

    def _run_stage(self, task_id: int, stage: str, progress: float, action):
        self._ensure_not_cancelled(task_id, stage)
        self.database.update_task_stage(task_id, stage, progress)
        self.database.log(task_id, stage, "INFO", f"开始阶段 {stage}")
        emit("task.progress", {"task_id": task_id, "stage": stage, "progress": progress})
        result = action()
        self.database.log(task_id, stage, "INFO", f"完成阶段 {stage}")
        self._ensure_not_cancelled(task_id, stage)
        return result

    def _ensure_not_cancelled(self, task_id: int, stage: str) -> None:
        if self.database.is_cancel_requested(task_id):
            raise CancellationRequested(stage)
