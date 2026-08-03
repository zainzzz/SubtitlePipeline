from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .event_bus import get_event_bus
from .logging_utils import setup_logging
from .model_manager import (
    DEFAULT_PROVIDER,
    PROVIDER_INFO,
    ModelManager,
    infer_provider_from_model_name,
    normalize_provider_name,
    resolve_model_name,
)
from .pipeline import (
    PipelineError,
    TaskContext,
    check_resume_feasibility,
    get_translation_provider,
    load_processed_segments,
    load_translations,
    normalize_stage_name,
    render_srt,
    save_translations,
)
from .runtime import ScannerService, WorkerService
from .store import Database
from .webhook import (
    TRIGGER_SUBTITLE_CHANGE,
    WebhookStatus,
    send_webhook_notification,
)


# In-process state for the subtitle-edit webhook path. Lives in the API process
# only — worker reads its own config snapshot when firing the completion hook.
_subtitle_webhook_lock = threading.Lock()
_subtitle_webhook_last_sent: dict[int, float] = {}


def _webhook_status_to_dict(status: WebhookStatus) -> dict[str, Any]:
    """Serialize a WebhookStatus for JSON responses."""
    return {
        "state": status.state,
        "webhook_type": status.webhook_type,
        "triggered_by": status.triggered_by,
        "error": status.error,
        "detail": status.detail,
    }


def _trigger_subtitle_webhook(task: dict[str, Any], config: dict[str, Any]) -> WebhookStatus:
    """Fire a webhook for a subtitle edit / re-render event.

    Two pre-conditions are checked before delegating to send_webhook_notification:
      1. `notification.trigger_on_subtitle_change` is on (default True).
      2. Debounce window has elapsed since the last send for this task_id.
         (Default 5s — coalesces rapid edits.)
    """
    notif = (config or {}).get("notification", {}) or {}
    if not notif.get("trigger_on_subtitle_change", True):
        return WebhookStatus(
            state="skipped",
            webhook_type=None,
            triggered_by=TRIGGER_SUBTITLE_CHANGE,
            detail="trigger_on_subtitle_change disabled",
        )

    debounce_seconds = int(notif.get("subtitle_change_debounce_seconds", 5) or 0)
    task_id = task.get("id")
    if not isinstance(task_id, int):
        return WebhookStatus(
            state="skipped",
            webhook_type=None,
            triggered_by=TRIGGER_SUBTITLE_CHANGE,
            detail="task missing id",
        )

    now = time.time()
    if debounce_seconds > 0:
        with _subtitle_webhook_lock:
            last = _subtitle_webhook_last_sent.get(task_id, 0.0)
            if now - last < debounce_seconds:
                return WebhookStatus(
                    state="skipped",
                    webhook_type=str(notif.get("webhook_type") or "") or None,
                    triggered_by=TRIGGER_SUBTITLE_CHANGE,
                    detail=f"debounced ({int(now - last)}s ago, window={debounce_seconds}s)",
                )
            _subtitle_webhook_last_sent[task_id] = now

    return send_webhook_notification(config, task, trigger=TRIGGER_SUBTITLE_CHANGE)


class ConfigUpdateRequest(BaseModel):
    file: dict[str, Any] | None = None
    processing: dict[str, Any] | None = None
    whisper: dict[str, Any] | None = None
    translation: dict[str, Any] | None = None
    subtitle: dict[str, Any] | None = None
    mux: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None
    notification: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    audio: dict[str, Any] | None = None


class RetryRequest(BaseModel):
    mode: Literal["restart", "resume"] = "restart"


class BatchRequest(BaseModel):
    task_ids: list[int]
    action: Literal["retry", "cancel", "delete"]


class SubtitleUpdateRequest(BaseModel):
    content: str


class ConfigImportRequest(BaseModel):
    config: dict[str, Any]
    version: int = 1


class SegmentsUpdateRequest(BaseModel):
    translations: dict[str, list[str]]


class RetryOverrideRequest(BaseModel):
    model_name: str | None = None
    provider: str | None = None
    target_languages: list[str] | None = None


class TaskActionResponse(BaseModel):
    id: int
    status: str
    stage: str
    progress: float
    cancel_requested: bool


class ScanResponse(BaseModel):
    scanned: int
    queued: int
    skipped: int


class TranslationTestRequest(BaseModel):
    enabled: bool = True
    llm_type: str = "openai-compatible"
    api_base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = 30
    target_language: str = "zh"
    content_type: str = "general"
    custom_prompt: str = ""


class SetupCompleteRequest(BaseModel):
    setup_complete: bool = True


class ManualTaskRequest(BaseModel):
    file_path: str


def get_proxy_status() -> dict[str, str | None]:
    return {
        "http_proxy": os.environ.get("HTTP_PROXY"),
        "https_proxy": os.environ.get("HTTPS_PROXY"),
        "hf_endpoint": os.environ.get("HF_ENDPOINT"),
    }


def resolve_db_path() -> str:
    return os.environ.get("SUBPIPELINE_DB_PATH", "/config/subpipeline.db")


def resolve_frontend_dist() -> Path:
    return Path(os.environ.get("SUBPIPELINE_FRONTEND_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))


def resolve_models_dir() -> str:
    return os.environ.get("SUBPIPELINE_MODELS_DIR", "/models")


def resolve_browse_roots() -> list[Path]:
    roots = [Path("/data"), Path("/output"), Path("/config")]
    extra = os.environ.get("SUBPIPELINE_BROWSE_ROOTS", "")
    for value in extra.split(","):
        stripped = value.strip()
        if stripped:
            roots.append(Path(stripped))
    resolved: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        candidate = root.expanduser().resolve()
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            resolved.append(candidate)
    return resolved


def is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_browse_target(raw_path: str | None) -> tuple[Path, list[Path]]:
    roots = resolve_browse_roots()
    requested = roots[0] if not raw_path or not raw_path.strip() else Path(raw_path).expanduser().resolve()
    for root in roots:
        if is_within_root(requested, root):
            return requested, roots
    raise HTTPException(status_code=403, detail="请求路径不在允许浏览的目录范围内")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database = Database(resolve_db_path())
    database.initialize()
    setup_logging(database)
    app.state.database = database
    app.state.model_manager = ModelManager(resolve_models_dir())
    yield
    database.close()


def get_database(app: FastAPI) -> Database:
    return app.state.database


def get_model_manager(app: FastAPI) -> ModelManager:
    return app.state.model_manager


def create_app() -> FastAPI:
    app = FastAPI(title="SubPipeline", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    frontend_dist = resolve_frontend_dist()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tasks")
    def list_tasks(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        status: str | None = Query(None),
        search: str | None = Query(None),
        date_from: str | None = Query(None),
        date_to: str | None = Query(None),
    ) -> dict[str, Any]:
        database = get_database(app)
        result = database.list_tasks(page=page, page_size=page_size, status=status, search=search, date_from=date_from, date_to=date_to)
        return {
            "items": result.items,
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
            "status_counts": result.status_counts,
        }

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.post("/api/tasks/{task_id}/cancel", response_model=TaskActionResponse)
    def cancel_task(task_id: int) -> TaskActionResponse:
        database = get_database(app)
        task = database.request_cancel(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found or not processing")
        return TaskActionResponse(
            id=task["id"],
            status=task["status"],
            stage=task["stage"],
            progress=task["progress"],
            cancel_requested=bool(task["cancel_requested"]),
        )

    @app.post("/api/tasks/{task_id}/retry", response_model=TaskActionResponse)
    def retry_task(task_id: int, request: RetryRequest) -> TaskActionResponse:
        database = get_database(app)
        try:
            task = database.request_retry(task_id, request.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not task:
            raise HTTPException(status_code=404, detail="task not found or not retryable")
        return TaskActionResponse(
            id=task["id"],
            status=task["status"],
            stage=task["stage"],
            progress=task["progress"],
            cancel_requested=bool(task["cancel_requested"]),
        )

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int) -> dict[str, Any]:
        database = get_database(app)
        if not database.delete_task(task_id):
            raise HTTPException(status_code=404, detail="task not found")
        return {"deleted": task_id}

    @app.get("/api/tasks/{task_id}/resume-check")
    def get_resume_check(task_id: int) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return check_resume_feasibility(task)

    @app.get("/api/tasks/{task_id}/logs")
    def get_task_logs(
        task_id: int,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        result = database.get_logs(task_id=task_id, page=page, page_size=page_size)
        return {
            "items": result.items,
            "page": result.page,
            "page_size": result.page_size,
            "total": result.total,
        }

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        database = get_database(app)
        return database.get_config()

    @app.put("/api/config")
    def update_config(request: ConfigUpdateRequest) -> dict[str, Any]:
        database = get_database(app)
        payload = request.model_dump(exclude_none=True)
        try:
            return database.update_config(payload)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/browse")
    def browse_directory(
        path: str | None = Query(None),
        mode: str = Query("directory", pattern="^(directory|file|both)$"),
    ) -> dict[str, Any]:
        target_path, roots = resolve_browse_target(path)
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="目录不存在")
        if not target_path.is_dir():
            raise HTTPException(status_code=400, detail="请求路径不是目录")
        parent = target_path.parent if any(is_within_root(target_path.parent, root) for root in roots) else None
        entries = list(target_path.iterdir())
        dirs = sorted(item.name for item in entries if item.is_dir())
        files: list[dict[str, Any]] = []
        if mode in ("file", "both"):
            config = get_database(app).get_config()
            allowed = {
                ext.lower() for ext in config["file"].get("allowed_extensions", [])
            } or {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".m4v"}
            for item in sorted(entries, key=lambda i: i.name.lower()):
                if item.is_file() and item.suffix.lower() in allowed:
                    stat = item.stat()
                    files.append({
                        "name": item.name,
                        "size_bytes": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
        return {
            "current": str(target_path),
            "parent": str(parent) if parent is not None else None,
            "dirs": dirs,
            "files": files,
        }

    @app.get("/api/system/status")
    def get_system_status() -> dict[str, Any]:
        database = get_database(app)
        model_manager = get_model_manager(app)
        config = database.get_config()
        system_status = database.get_system_status()
        current_provider = normalize_provider_name(
            config["whisper"].get("provider")
            or infer_provider_from_model_name(str(config["whisper"]["model_name"]), DEFAULT_PROVIDER)
        )
        current_model = resolve_model_name(str(config["whisper"]["model_name"]), current_provider)
        return {
            **system_status,
            "asr_ready": model_manager.has_model(current_model, current_provider),
            "current_model": current_model,
            "current_provider": current_provider,
            "proxy": get_proxy_status(),
        }

    @app.post("/api/system/setup-complete")
    def set_setup_complete(request: SetupCompleteRequest) -> dict[str, Any]:
        database = get_database(app)
        updated = database.set_setup_complete(request.setup_complete)
        config = database.get_config()
        model_manager = get_model_manager(app)
        current_provider = normalize_provider_name(
            config["whisper"].get("provider")
            or infer_provider_from_model_name(str(config["whisper"]["model_name"]), DEFAULT_PROVIDER)
        )
        current_model = resolve_model_name(str(config["whisper"]["model_name"]), current_provider)
        return {
            **updated,
            "asr_ready": model_manager.has_model(current_model, current_provider),
            "current_model": current_model,
            "current_provider": current_provider,
            "proxy": get_proxy_status(),
        }

    @app.post("/api/translation/test")
    def test_translation(request: TranslationTestRequest) -> dict[str, Any]:
        if not request.enabled:
            return {"success": True, "message": "翻译已禁用，跳过连接测试"}
        try:
            provider = get_translation_provider(
                {
                    "translation": {
                        "enabled": True,
                        "llm_type": request.llm_type,
                        "api_base_url": request.api_base_url,
                        "api_key": request.api_key,
                        "model": request.model,
                        "timeout_seconds": request.timeout_seconds,
                        "content_type": request.content_type,
                        "custom_prompt": request.custom_prompt,
                    }
                }
            )
            provider.translate_batch(["connection check"], request.target_language)
        except Exception as exc:
            return {"success": False, "message": str(exc)}
        return {"success": True, "message": "翻译服务连接成功"}

    @app.get("/api/models")
    def list_models() -> dict[str, Any]:
        database = get_database(app)
        model_manager = get_model_manager(app)
        config = database.get_config()
        current_provider = normalize_provider_name(
            config["whisper"].get("provider")
            or infer_provider_from_model_name(str(config["whisper"]["model_name"]), DEFAULT_PROVIDER)
        )
        current_model = resolve_model_name(str(config["whisper"]["model_name"]), current_provider)
        return {
            "items": model_manager.list_models(current_model),
            "current_model": current_model,
            "current_provider": current_provider,
            "providers": PROVIDER_INFO,
        }

    @app.post("/api/models/{name}/download", status_code=202)
    def download_model(name: str) -> dict[str, str]:
        model_manager = get_model_manager(app)
        try:
            model_manager.start_download(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"message": f"模型 {name} 下载已启动"}

    @app.delete("/api/models/{name}")
    def delete_model(name: str) -> dict[str, str]:
        database = get_database(app)
        model_manager = get_model_manager(app)
        config = database.get_config()
        current_provider = normalize_provider_name(
            config["whisper"].get("provider")
            or infer_provider_from_model_name(str(config["whisper"]["model_name"]), DEFAULT_PROVIDER)
        )
        current_model = resolve_model_name(str(config["whisper"]["model_name"]), current_provider)
        try:
            model_manager.delete_model(name, current_model)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"message": f"模型 {name} 已删除"}

    @app.post("/api/models/{name}/activate")
    def activate_model(name: str) -> dict[str, Any]:
        database = get_database(app)
        model_manager = get_model_manager(app)
        provider = infer_provider_from_model_name(name, DEFAULT_PROVIDER)
        canonical_name = resolve_model_name(name, provider)
        if not model_manager.has_model(canonical_name, provider):
            raise HTTPException(status_code=400, detail="模型尚未安装，无法切换")
        try:
            updated = database.update_config({"whisper": {"model_name": canonical_name, "provider": provider}})
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "message": f"当前模型已切换为 {canonical_name}",
            "config": updated,
        }

    @app.post("/api/admin/scans/run", response_model=ScanResponse)
    def run_scan_once() -> ScanResponse:
        database = get_database(app)
        result = ScannerService(database).scan_once()
        return ScanResponse(scanned=result.scanned, queued=result.queued, skipped=result.skipped)

    @app.get("/api/admin/scans/status")
    def get_scan_status() -> dict[str, Any]:
        database = get_database(app)
        config = database.get_config()
        status = database.get_scan_status()
        return {
            **status,
            "scan_enabled": bool(config["file"].get("scan_enabled", True)),
            "pending_count": database.count_tasks_by_status("pending"),
        }

    @app.patch("/api/admin/scans/status")
    def toggle_scan_status(request: dict[str, bool]) -> dict[str, Any]:
        """Toggle or set scan_enabled state directly, bypassing full config merge."""
        database = get_database(app)
        scan_enabled = request.get("scan_enabled")
        if scan_enabled is None:
            raise HTTPException(status_code=400, detail="scan_enabled is required")
        database.update_config({"file": {"scan_enabled": scan_enabled}})
        return get_scan_status()

    @app.post("/api/admin/work/run-next")
    def run_next_task() -> dict[str, bool]:
        database = get_database(app)
        processed = WorkerService(database).process_next_task()
        return {"processed": processed}

    # ---- Dashboard ----
    @app.get("/api/dashboard/stats")
    def get_dashboard_stats() -> dict[str, Any]:
        database = get_database(app)
        return database.get_dashboard_stats()

    @app.get("/api/dashboard/suspect-tasks")
    def get_suspect_tasks(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        """List done tasks whose post-pipeline quality check flagged them as suspect.

        Returns up to `limit` items (default 20, max 200) ordered by finished_at desc.
        Each item includes the full `quality_report` so the UI can show score,
        issues, and suspect segment indices without an extra round trip.
        """
        database = get_database(app)
        items = database.get_suspect_tasks(limit=limit)
        return {"items": items, "count": len(items)}

    # ---- Batch operations ----
    @app.post("/api/tasks/batch")
    def batch_tasks(request: BatchRequest) -> dict[str, Any]:
        database = get_database(app)
        ids = request.task_ids
        if not ids:
            raise HTTPException(status_code=400, detail="task_ids is required")
        if request.action == "retry":
            results = database.batch_retry(ids)
            return {"results": results}
        elif request.action == "cancel":
            count = database.batch_cancel(ids)
            return {"cancelled": count}
        elif request.action == "delete":
            count = database.batch_delete(ids)
            return {"deleted": count}
        raise HTTPException(status_code=400, detail="unknown action")

    # ---- Subtitle preview & edit ----
    @app.get("/api/tasks/{task_id}/subtitle")
    def get_task_subtitle(task_id: int) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        result_payload = task.get("result_payload")
        if not result_payload or "subtitle_paths" not in result_payload:
            raise HTTPException(status_code=404, detail="no subtitle files found for this task")
        subtitle_path = result_payload["subtitle_paths"][0]
        path = Path(subtitle_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="subtitle file not found on disk")
        content = path.read_text(encoding="utf-8")
        return {"content": content, "path": subtitle_path}

    @app.put("/api/tasks/{task_id}/subtitle")
    def update_task_subtitle(task_id: int, request: SubtitleUpdateRequest) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        result_payload = task.get("result_payload")
        if not result_payload or "subtitle_paths" not in result_payload:
            raise HTTPException(status_code=404, detail="no subtitle files found for this task")
        for subtitle_path in result_payload["subtitle_paths"]:
            path = Path(subtitle_path)
            if path.exists():
                path.write_text(request.content, encoding="utf-8")
        # Re-read config from DB (not from cached task) so webhook settings reflect
        # any recent changes the user made on the settings page.
        config = database.get_config()
        webhook_status = _trigger_subtitle_webhook(task, config)
        return {
            "status": "updated",
            "webhook": _webhook_status_to_dict(webhook_status),
        }

    @app.get("/api/tasks/{task_id}/segments")
    def get_task_segments(task_id: int) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] != "done":
            raise HTTPException(status_code=400, detail="task not yet completed")
        config = task.get("config_snapshot") or database.get_config()
        work_dir = Path(config["processing"]["work_dir"]) / str(task_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        context = TaskContext(
            task_id=task_id,
            file_path=task["file_path"],
            config_snapshot=config,
            work_dir=work_dir,
        )
        try:
            processed = load_processed_segments(context)
        except PipelineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            translations = load_translations(context)
        except PipelineError:
            translations = {}
        return {"segments": processed, "translations": translations}

    @app.put("/api/tasks/{task_id}/segments")
    def update_task_segments(task_id: int, request: SegmentsUpdateRequest) -> dict[str, str]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] != "done":
            raise HTTPException(status_code=400, detail="task not yet completed")
        config = task.get("config_snapshot") or database.get_config()
        work_dir = Path(config["processing"]["work_dir"]) / str(task_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        context = TaskContext(
            task_id=task_id,
            file_path=task["file_path"],
            config_snapshot=config,
            work_dir=work_dir,
        )
        try:
            processed = load_processed_segments(context)
        except PipelineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        segment_count = len(processed)
        for lang, lines in request.translations.items():
            if len(lines) != segment_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"language '{lang}' has {len(lines)} lines, expected {segment_count}",
                )
        save_translations(context, request.translations)
        subtitle_paths = render_srt(context, processed, request.translations)
        # Fire the subtitle-change webhook after the SRT files have been
        # re-rendered, so the media server refresh picks up the latest content.
        config = database.get_config()
        webhook_status = _trigger_subtitle_webhook(task, config)
        return {
            "status": "updated",
            "subtitle_paths": subtitle_paths,
            "webhook": _webhook_status_to_dict(webhook_status),
        }

    # ---- Re-translate (re-run translation for done task) ----
    @app.post("/api/tasks/{task_id}/retranslate")
    def retranslate_task(task_id: int, request: RetryOverrideRequest | None = None) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] != "done":
            raise HTTPException(status_code=400, detail="only done tasks can be retranslated")
        config_snapshot = task.get("config_snapshot") or database.get_config()
        if not config_snapshot.get("translation", {}).get("enabled"):
            raise HTTPException(status_code=400, detail="translation is disabled in task config")
        if request and request.target_languages:
            config_snapshot = {**config_snapshot, "translation": {**config_snapshot.get("translation", {}), "target_languages": list(request.target_languages)}}
        database.requeue_with_new_config(task_id, "translate", config_snapshot)
        emit("task.requeued", {"task_id": task_id, "reason": "retranslate"})
        return {"status": "queued", "task_id": task_id}

    # ---- Manual subtitle-change webhook re-trigger ----
    @app.post("/api/tasks/{task_id}/webhook/trigger")
    def trigger_subtitle_webhook(task_id: int) -> dict[str, Any]:
        """Manually fire a subtitle-change webhook for a done task.

        Bypasses the in-process debounce window so the user can force a re-send
        if the last attempt failed. No-op (with a 'skipped' status) if the
        trigger is disabled or the task is not in a state that supports it.
        """
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] != "done":
            raise HTTPException(status_code=400, detail="only done tasks support manual webhook trigger")
        config = database.get_config()
        # Clear the debounce record so this manual send isn't suppressed.
        with _subtitle_webhook_lock:
            _subtitle_webhook_last_sent[int(task["id"])] = 0.0
        status = _trigger_subtitle_webhook(task, config)
        return {"webhook": _webhook_status_to_dict(status)}

    # ---- Retry with model override ----
    @app.post("/api/tasks/{task_id}/retry-with-model")
    def retry_with_model(task_id: int, request: RetryOverrideRequest) -> dict[str, Any]:
        database = get_database(app)
        task = database.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        if task["status"] not in {"failed", "cancelled", "done"}:
            raise HTTPException(status_code=400, detail="task is not retryable")
        config_snapshot = task.get("config_snapshot") or database.get_config()
        whisper = dict(config_snapshot.get("whisper", {}))
        if request.model_name:
            whisper["model_name"] = request.model_name
        if request.provider:
            whisper["provider"] = request.provider
        if request.target_languages is not None:
            translation = dict(config_snapshot.get("translation", {}))
            translation["target_languages"] = list(request.target_languages)
            config_snapshot = {**config_snapshot, "translation": translation, "whisper": whisper}
        else:
            config_snapshot = {**config_snapshot, "whisper": whisper}
        feasibility = check_resume_feasibility(task)
        if feasibility.get("can_resume") and request.model_name is None and request.provider is None:
            resume_stage = str(feasibility.get("resume_stage", "queued"))
        else:
            resume_stage = "extract_audio"
        from .pipeline import normalize_stage_name as _ns
        database.requeue_with_new_config(task_id, _ns(resume_stage), config_snapshot)
        emit("task.requeued", {"task_id": task_id, "reason": "retry_with_model"})
        return {"status": "queued", "task_id": task_id, "stage": resume_stage}

    # ---- Config import/export ----
    @app.get("/api/config/export")
    def export_config() -> dict[str, Any]:
        database = get_database(app)
        config = database.get_config()
        config.pop("meta", None)
        return {"config": config, "version": 1}

    @app.post("/api/config/import")
    def import_config(request: ConfigImportRequest) -> dict[str, Any]:
        database = get_database(app)
        payload = {k: v for k, v in request.config.items() if k != "meta"}
        try:
            return database.update_config(payload)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- SSE ----
    @app.get("/api/events")
    async def sse_events():
        bus = get_event_bus()
        q = bus.subscribe()

        async def event_stream():
            try:
                yield f"data: {json.dumps({'type': 'connected', 'timestamp': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=30)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # ---- Manual task creation ----
    @app.post("/api/tasks/manual")
    def create_manual_task(request: ManualTaskRequest) -> dict[str, Any]:
        database = get_database(app)
        file_path = request.file_path.strip()
        if not file_path:
            raise HTTPException(status_code=400, detail="file_path is required")
        path = Path(file_path)
        if not path.is_absolute():
            raise HTTPException(status_code=400, detail="file_path must be absolute")
        if not path.exists():
            raise HTTPException(status_code=404, detail="file not found")
        roots = resolve_browse_roots()
        if not any(is_within_root(path.resolve(), root) for root in roots):
            raise HTTPException(status_code=403, detail="file is not within allowed directories")
        stat = path.stat()
        observed = database.observe_file(str(path), int(stat.st_size), float(stat.st_mtime))
        path_key = observed["path_key"]
        if database.has_active_task(path_key):
            raise HTTPException(status_code=409, detail="task already exists for this file")
        task = database.create_task(observed["file_id"], observed["path"], observed["size_bytes"], observed["mtime"])
        return {"task": task}

    # ---- Process health check ----
    @app.get("/api/system/process-health")
    def get_process_health() -> dict[str, Any]:
        from pathlib import Path as _Path
        result: dict[str, Any] = {"scanner": {"running": False, "pid": None}, "worker": {"running": False, "pid": None}, "api": {"running": False, "pid": None}}
        for entry in _Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            except (OSError, FileNotFoundError):
                continue
            pid = entry.name
            if "app.api_server" in cmdline and "sh" not in cmdline[:3]:
                result["api"] = {"running": True, "pid": pid}
            elif "app.scanner_process" in cmdline:
                result["scanner"] = {"running": True, "pid": pid}
            elif "app.worker_process" in cmdline:
                result["worker"] = {"running": True, "pid": pid}
        result["sse_subscribers"] = get_event_bus().subscriber_count()
        result["all_healthy"] = result["scanner"]["running"] and result["worker"]["running"] and result["api"]["running"]
        return result

    if frontend_dist.exists():
        assets_dir = frontend_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            # 防 path traversal:resolve 后必须仍在 frontend_dist 内,否则 404
            resolved_root = frontend_dist.resolve()
            candidate = (frontend_dist / full_path).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                raise HTTPException(status_code=404, detail="not found")
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            index_path = frontend_dist / "index.html"
            if index_path.exists():
                return FileResponse(index_path)
            raise HTTPException(status_code=404, detail="frontend not built")

    return app


app = create_app()
