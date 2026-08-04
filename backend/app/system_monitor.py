"""Pre-ASR resource checks for the worker.

Goal: refuse (or warn) before launching Whisper / Qwen on a video that's
going to OOM the GPU or fill the work_dir disk. The user experience we
care about is "self-hosted NAS with limited VRAM + a small SSD" — the
worker should not silently lock up the box because a 20 GB movie got
queued against a 4 GB VRAM GPU.

Design:
- Pure-Python checks that defer to the standard library (`shutil.disk_usage`)
  and a soft import of `torch` (for CUDA memory query). No failure mode
  raises — every error path returns a degraded result with a human-readable
  reason.
- The memory estimate is a static table (ASR model family → rough VRAM
  need). Real VRAM is model + audio-length dependent, but a coarse
  estimate catches the 90% "I queued a 4-hour movie against a 4 GB card"
  case.
- Returns a `ResourceCheck` dataclass that the worker can inspect to
  decide between three actions:
    1. HARD BLOCK (refuse, mark task failed) — required + missing
    2. SOFT WARN (log a warning, continue) — recommended + missing
    3. NO-OP — everything fits
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Rough VRAM requirements per ASR model family. These are conservative
# upper bounds (the "fits everything" budget) for the model in inference
# mode. They do NOT account for the on-disk whisperx alignment model
# (small) or the qwen forced aligner (larger). Tuned by hand from
# community reports — not authoritative.
_MODEL_VRAM_MB: dict[str, int] = {
    "whisperx-tiny": 1500,
    "whisperx-base": 1500,
    "whisperx-small": 2000,
    "whisperx-medium": 3000,
    "whisperx-large-v2": 5000,
    "whisperx-large-v3": 5000,
    "faster-whisper-tiny": 1500,
    "faster-whisper-base": 1500,
    "faster-whisper-small": 2000,
    "faster-whisper-medium": 3000,
    "faster-whisper-large-v2": 4500,
    "faster-whisper-large-v3": 4500,
    "faster-whisper-large-v3-turbo": 4000,
    "anime-whisper": 2000,
    "qwen3-asr-0.6b": 2000,
    "qwen3-asr-1.7b": 4000,
    "qwen3-asr-3b": 6000,
}

# Disk headroom: 1.5x audio size is a safe upper bound for "audio.wav +
# intermediate JSON + final SRT". Rounded up to a 1 GB floor so small
# files still get checked.
_MIN_DISK_HEADROOM_MB = 1024
_AUDIO_SIZE_MULTIPLIER = 1.5


@dataclass
class ResourceCheck:
    """Aggregate pre-ASR resource check result.

    Attributes:
        ok: True when both GPU and disk are sufficient (or when the check
            is disabled / unavailable). When False, the task should NOT
            proceed to ASR.
        gpu_required_mb: Estimated VRAM required for the selected model.
            None when GPU check is not available (e.g. CPU-only / no torch).
        gpu_available_mb: Free VRAM reported by torch. None when not available.
        gpu_reason: Human-readable explanation when GPU check fails or is
            unavailable. Empty string when everything fits.
        disk_required_mb: Estimated disk space needed in work_dir.
        disk_available_mb: Free bytes on the work_dir filesystem.
        disk_reason: Human-readable explanation when disk check fails.
        recommendations: List of suggested fixes the user can take. Always
            present, possibly empty.
        warnings: Soft warnings (block the task but list them so the
            UI can show them in the failure reason). Empty when ok.
    """

    ok: bool
    gpu_required_mb: int | None = None
    gpu_available_mb: int | None = None
    gpu_reason: str = ""
    disk_required_mb: int | None = None
    disk_available_mb: int | None = None
    disk_reason: str = ""
    recommendations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_vram_mb(model_name: str) -> int:
    """Return the estimated VRAM (MB) needed to run `model_name` for ASR.

    Falls back to 3000 MB (a reasonable mid-range ASR budget) when the model
    is not in the table. The estimate is a soft target — actual usage will
    be lower for short audio and higher for long audio."""
    if not model_name:
        return 3000
    key = model_name.strip().lower()
    if key in _MODEL_VRAM_MB:
        return _MODEL_VRAM_MB[key]
    # Fall back to a family-based lookup (e.g. "faster-whisper-base" prefix
    # lookup). This handles downstream re-taggings of the same model.
    for prefix, mb in _MODEL_VRAM_MB.items():
        if key.startswith(prefix):
            return mb
    return 3000


def estimate_disk_mb(audio_size_bytes: int) -> int:
    """Return the estimated disk space (MB) needed in work_dir for the
    intermediate artifacts of one task (audio + JSON + SRT outputs)."""
    audio_mb = max(0, int(audio_size_bytes) / (1024 * 1024))
    return max(_MIN_DISK_HEADROOM_MB, int(audio_mb * _AUDIO_SIZE_MULTIPLIER) + 200)


def get_gpu_free_mb() -> int | None:
    """Return free VRAM in MB, or None if torch / CUDA is unavailable.

    A failure (no torch, no CUDA, query error) is NOT an error: it just
    means we can't tell, so the caller should treat the check as a
    soft-warning rather than a hard fail."""
    try:
        import torch  # noqa: PLC0415  (soft import)
    except Exception:
        return None
    try:
        if not torch.cuda.is_available():
            return None
        free_bytes, _total_bytes = torch.cuda.mem_get_info()
        return int(free_bytes / (1024 * 1024))
    except Exception as exc:
        logger.debug("get_gpu_free_mb: torch query failed: %s", exc)
        return None


def get_disk_free_mb(path: str | Path) -> int | None:
    """Return free bytes (in MB) on the filesystem backing `path`.

    Returns None on permission errors or non-existent paths so the caller
    can fall back to "skip the check" rather than crash."""
    try:
        usage = shutil.disk_usage(str(path))
        return int(usage.free / (1024 * 1024))
    except Exception as exc:
        logger.debug("get_disk_free_mb: statvfs failed for %s: %s", path, exc)
        return None


def check_resources(
    model_name: str,
    audio_size_bytes: int,
    work_dir: str | Path,
    *,
    config: dict[str, Any] | None = None,
) -> ResourceCheck:
    """Run the pre-ASR resource battery.

    Args:
        model_name: ASR model identifier (e.g. "whisperx-small").
        audio_size_bytes: Size of the extracted audio (or video, as proxy)
            in bytes. The disk estimate is derived from this.
        work_dir: Where the worker will write intermediates.
        config: Optional app config; reads `processing.pre_asr_resource_check`
            and `processing.resource_headroom_pct` if present.

    Returns:
        ResourceCheck. Never raises. The caller (worker) inspects `.ok` to
        decide whether to proceed to the ASR stage.
    """
    cfg = (config or {}).get("processing", {}) or {}
    if not cfg.get("pre_asr_resource_check", True):
        return ResourceCheck(ok=True, gpu_reason="check disabled", disk_reason="check disabled")

    headroom_pct = float(cfg.get("resource_headroom_pct", 20))
    if headroom_pct < 0:
        headroom_pct = 0
    if headroom_pct > 80:
        headroom_pct = 80

    check = ResourceCheck(ok=True)
    check.gpu_required_mb = estimate_vram_mb(model_name)
    check.disk_required_mb = estimate_disk_mb(audio_size_bytes)

    # --- GPU check ---
    gpu_free = get_gpu_free_mb()
    check.gpu_available_mb = gpu_free
    if gpu_free is not None:
        # Add headroom margin: if a 4 GB card is fully free but other
        # processes might claim 1 GB while we're running, require
        # (required * (1 + headroom%)) available.
        adjusted_required = int(check.gpu_required_mb * (1 + headroom_pct / 100.0))
        if gpu_free < adjusted_required:
            check.ok = False
            check.gpu_reason = (
                f"GPU 显存不足：需要约 {adjusted_required} MB（含 {headroom_pct:.0f}% 余量），"
                f"当前可用 {gpu_free} MB"
            )
            check.recommendations.extend([
                f"切换到更小的 ASR 模型（当前 {model_name} 需要约 {check.gpu_required_mb} MB）",
                "暂时关闭其他占用 GPU 的进程",
                "在 设置 → Whisper → device 切到 cpu（速度会慢很多）",
            ])
        else:
            check.gpu_reason = f"GPU 显存 {gpu_free} MB ≥ 需求 {adjusted_required} MB"
    else:
        check.gpu_reason = "未检测到 GPU（CPU 模式或 torch 未安装），跳过显存检查"

    # --- Disk check ---
    disk_free = get_disk_free_mb(work_dir)
    check.disk_available_mb = disk_free
    if disk_free is not None:
        adjusted_required = int(check.disk_required_mb * (1 + headroom_pct / 100.0))
        if disk_free < adjusted_required:
            check.ok = False
            check.disk_reason = (
                f"工作目录磁盘空间不足：需要约 {adjusted_required} MB（含 {headroom_pct:.0f}% 余量），"
                f"当前可用 {disk_free} MB"
            )
            check.recommendations.extend([
                f"清理 work_dir ({work_dir}) 下的旧任务产物（设置 → 处理 → keep_intermediates=false）",
                "把 work_dir 指向更大的磁盘",
            ])
        else:
            check.disk_reason = f"工作目录 {work_dir} 可用 {disk_free} MB ≥ 需求 {adjusted_required} MB"
    else:
        check.disk_reason = f"无法读取 {work_dir} 磁盘信息（权限或路径不存在），跳过检查"

    return check


def is_sufficient(check: ResourceCheck | dict[str, Any] | None) -> bool:
    """Convenience predicate. Returns True when the check is missing, a
    no-op, or a pass; False only when the worker should refuse to start."""
    if check is None:
        return True
    if isinstance(check, dict):
        return bool(check.get("ok", True))
    return check.ok
