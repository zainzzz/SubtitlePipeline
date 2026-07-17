from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.segment_cleaner import clean_segments

if TYPE_CHECKING:
    from app.store import Database

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT_SECONDS = 7200


# ---------------------------------------------------------------------------
# Core exceptions — MUST be defined before importing translation.py, which
# subclasses ``PipelineError`` for ``TranslationRateLimitError``.
# ---------------------------------------------------------------------------


class PipelineError(RuntimeError):
    pass


class CancellationRequested(RuntimeError):
    pass


@dataclass
class TaskContext:
    task_id: int
    file_path: str
    config_snapshot: dict[str, Any]
    work_dir: Path
    intermediates_dir: Path | None = None
    using_fallback_intermediates: bool = False


# ---------------------------------------------------------------------------
# Re-export translation + SRT helpers (extracted modules).
#
# The import is deliberately placed *after* ``PipelineError`` is defined so
# that ``app.llm.translation`` (which does ``from app.pipeline import
# PipelineError``) sees the symbol on the partially-initialised module.
# ---------------------------------------------------------------------------

from .asr import (  # noqa: E402
    ASRProvider,
    ASRProviderFactory,
    AnimeWhisperProvider,
    FasterWhisperProvider,
    QwenASRProvider,
    WhisperModelCache,
    WhisperXProvider,
    run_asr,
)
from .asr.aligners import QwenForcedAligner, WhisperXAligner  # noqa: E402
from .asr.helpers import get_models_root  # noqa: E402
from .llm import LLMError, LLMMessage, LLMRateLimitError, create_llm_client  # noqa: E402
from .llm.translation import (  # noqa: E402
    CHUNK_SIZE,
    CONTEXT_SIZE,
    FORMAT_INSTRUCTION,
    MAX_CHUNK_RETRIES,
    MAX_CHUNK_WORKERS,
    MAX_PARTIAL_RETRIES,
    TRANSLATION_PRESETS,
    ChunkLine,
    ChunkedTranslator,
    LLMTranslationProvider,
    ParseResult,
    TranslationChunk,
    TranslationProvider,
    TranslationRateLimitError,
    _translation_cache_key,
    build_chunk_user_message,
    build_chunks,
    debug_translation_request,
    parse_chunk_output,
    parse_numbered_lines,
    parse_numbered_lines_ordered,
    strip_code_fence,
    strip_number_prefix,
    strip_think,
)
from .model_manager import DEFAULT_PROVIDER, infer_provider_from_model_name, normalize_provider_name  # noqa: E402
from .subtitle.srt import (  # noqa: E402
    build_srt_content,
    format_srt_time,
    read_stage_artifacts,
    write_stage_artifacts,
)


STAGE_ALIAS_MAP = {
    "asr": "run_asr",
}
STAGE_PROGRESS = {
    "extract_audio": 10,
    "run_asr": 35,
    "align_segments": 45,
    "text_process": 55,
    "translate": 60,
    "subtitle_render": 95,
    "output_finalize": 100,
    "mux": 100,
}


def normalize_stage_name(stage: str) -> str:
    return STAGE_ALIAS_MAP.get(str(stage), str(stage))


# ---------------------------------------------------------------------------
# Intermediate-file helpers
# ---------------------------------------------------------------------------


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def get_intermediates_dir(source_path: Path) -> Path:
    return source_path.parent / ".subpipeline" / source_path.stem


def _intermediate_filenames() -> tuple[str, ...]:
    return ("audio.wav", "asr_result.json", "aligned_segments.json", "processed_segments.json", "translations.json")


def ensure_intermediates_dir(context: TaskContext) -> Path:
    if context.intermediates_dir is not None:
        return context.intermediates_dir
    target_dir = get_intermediates_dir(Path(context.file_path))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        context.intermediates_dir = target_dir
        context.using_fallback_intermediates = False
    except OSError:
        context.work_dir.mkdir(parents=True, exist_ok=True)
        context.intermediates_dir = context.work_dir
        context.using_fallback_intermediates = True
    return context.intermediates_dir


def resolve_intermediates_dir(context: TaskContext) -> Path:
    if context.intermediates_dir is not None:
        return context.intermediates_dir
    target_dir = get_intermediates_dir(Path(context.file_path))
    if target_dir.exists():
        context.intermediates_dir = target_dir
        context.using_fallback_intermediates = False
        return target_dir
    context.intermediates_dir = context.work_dir
    context.using_fallback_intermediates = True
    return context.work_dir


def get_intermediate_path(context: TaskContext, filename: str, create: bool = False) -> Path:
    base_dir = ensure_intermediates_dir(context) if create else resolve_intermediates_dir(context)
    return base_dir / filename


def cleanup_intermediates(source_path: Path) -> None:
    shutil.rmtree(get_intermediates_dir(source_path), ignore_errors=True)


def cleanup_work_dir_intermediates(work_dir: Path) -> None:
    for filename in _intermediate_filenames():
        path = work_dir / filename
        if path.exists():
            path.unlink()


def _write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_ffmpeg(command: list[str], timeout_message: str, failure_message: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(timeout_message) from exc
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or failure_message)
    return result


# ---------------------------------------------------------------------------
# Pipeline stage: extract_audio
# ---------------------------------------------------------------------------


def extract_audio(context: TaskContext) -> Path:
    whisper_config = context.config_snapshot["whisper"]
    source_path = Path(context.file_path)
    audio_format = str(whisper_config["audio_format"]).strip().lower()
    audio_path = get_intermediate_path(context, f"audio.{audio_format}", create=True)
    ensure_parent(audio_path)
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise PipelineError("ffmpeg 未安装，无法执行真实音频提取")
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(whisper_config["sample_rate"]),
        str(audio_path),
    ]
    _run_ffmpeg(command, f"音频提取超时，FFmpeg 执行超过 {FFMPEG_TIMEOUT_SECONDS} 秒", "ffmpeg 执行失败")
    return audio_path


# ---------------------------------------------------------------------------
# Pipeline stage: run_asr (save / load intermediates)
# ---------------------------------------------------------------------------


def save_asr_result(context: TaskContext, payload: dict[str, Any]) -> Path:
    path = get_intermediate_path(context, "asr_result.json", create=True)
    _write_json(path, payload)
    return path


def load_asr_result(context: TaskContext) -> dict[str, Any]:
    path = get_intermediate_path(context, "asr_result.json")
    if not path.exists():
        raise PipelineError("缺少 asr_result.json，无法继续执行")
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise PipelineError("asr_result.json 内容无效，无法继续执行")
    return payload


def save_aligned_segments(context: TaskContext, payload: list[dict[str, Any]]) -> Path:
    path = get_intermediate_path(context, "aligned_segments.json", create=True)
    _write_json(path, payload)
    return path


def load_aligned_segments(context: TaskContext) -> list[dict[str, Any]]:
    path = get_intermediate_path(context, "aligned_segments.json")
    if not path.exists():
        raise PipelineError("缺少 aligned_segments.json，无法继续执行")
    payload = _read_json(path)
    if not isinstance(payload, list):
        raise PipelineError("aligned_segments.json 内容无效，无法继续执行")
    return payload


# ---------------------------------------------------------------------------
# Pipeline stage: align_segments
# ---------------------------------------------------------------------------


def _resolve_align_provider(config_snapshot: dict[str, Any]) -> str:
    whisper_config = config_snapshot.get("whisper", {})
    return str(whisper_config.get("align_provider", whisper_config.get("align_method", "auto"))).strip().lower() or "auto"


def _has_qwen_forced_aligner_model() -> bool:
    model_dir = get_models_root() / "qwen3-forced-aligner"
    return model_dir.exists() and any(model_dir.iterdir())


def align_segments(
    context: TaskContext,
    asr_result: dict[str, Any],
    audio_path: Path,
    model_cache: WhisperModelCache | None = None,
    database: Any = None,
) -> list[dict[str, Any]]:
    original_segments = asr_result.get("segments", [])
    normalized_segments = [
        {
            "start": float(segment.get("start", 0.0)),
            "end": float(segment.get("end", segment.get("start", 0.0))),
            "text": str(segment.get("text", "")).strip(),
        }
        for segment in original_segments
    ]
    align_provider = _resolve_align_provider(context.config_snapshot)
    asr_provider = normalize_provider_name(
        asr_result.get("provider")
        or context.config_snapshot["whisper"].get("provider")
        or infer_provider_from_model_name(str(context.config_snapshot["whisper"].get("model_name", "")), DEFAULT_PROVIDER)
    )
    language = str(asr_result.get("language", "")).strip() or None

    if align_provider == "none":
        if database is not None:
            database.log(context.task_id, "align_segments", "INFO", "已跳过时间轴对齐，直接使用 ASR 时间戳")
        return normalized_segments

    selected_provider = align_provider
    if align_provider == "auto":
        if asr_provider == "whisperx":
            selected_provider = "whisperx"
        elif _has_qwen_forced_aligner_model():
            selected_provider = "qwen-forced"
        else:
            if database is not None:
                database.log(
                    context.task_id,
                    "align_segments",
                    "WARNING",
                    "建议下载 qwen3-forced-aligner 以提升时间戳精度，当前使用内置 timestamps 继续",
                )
            return normalized_segments

    if selected_provider == "whisperx":
        return WhisperXAligner(model_cache).align(normalized_segments, audio_path, language, context.config_snapshot["whisper"]["device"])
    if selected_provider == "qwen-forced":
        return QwenForcedAligner().align(normalized_segments, audio_path, language, context.config_snapshot["whisper"]["device"])
    raise PipelineError(f"不支持的对齐 Provider: {selected_provider}")


# ---------------------------------------------------------------------------
# Pipeline stage: process_text_segments
# ---------------------------------------------------------------------------


def process_text_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Step 1: Clean segments (remove repetition loops, merge fragments, split long segments, etc.)
    cleaned = clean_segments(segments)

    # Step 2: Normalize whitespace and add trailing punctuation
    processed: list[dict[str, Any]] = []
    for segment in cleaned:
        text = " ".join(str(segment["text"]).split())
        if text and text[-1] not in ".!?。！？":
            text = f"{text}."
        processed.append(
            {
                "start": float(segment["start"]),
                "end": float(segment["end"]),
                "text": text,
            }
        )
    return processed


def save_processed_segments(context: TaskContext, payload: list[dict[str, Any]]) -> Path:
    path = get_intermediate_path(context, "processed_segments.json", create=True)
    _write_json(path, payload)
    return path


def load_processed_segments(context: TaskContext) -> list[dict[str, Any]]:
    path = get_intermediate_path(context, "processed_segments.json")
    if not path.exists():
        raise PipelineError("缺少 processed_segments.json，无法继续执行")
    payload = _read_json(path)
    if not isinstance(payload, list):
        raise PipelineError("processed_segments.json 内容无效，无法继续执行")
    return payload


# ---------------------------------------------------------------------------
# Pipeline stage: translate_segments
# ---------------------------------------------------------------------------


def get_translation_provider(config_snapshot: dict[str, Any]) -> TranslationProvider:
    translation = config_snapshot["translation"]
    return LLMTranslationProvider(
        llm_type=str(translation.get("llm_type", "openai-compatible")).strip(),
        api_base_url=str(translation["api_base_url"]).strip(),
        api_key=str(translation["api_key"]).strip(),
        model=str(translation["model"]).strip(),
        timeout_seconds=int(translation["timeout_seconds"]),
    )


def translate_segments(
    context: TaskContext,
    segments: list[dict[str, Any]],
    progress_callback=None,
    database: Database | None = None,
) -> dict[str, list[str]]:
    translation_config = context.config_snapshot["translation"]
    if not translation_config["enabled"]:
        return {}
    provider = get_translation_provider(context.config_snapshot)
    translations: dict[str, list[str]] = {}
    max_retries = max(int(translation_config["max_retries"]), 1)
    target_languages = [str(language) for language in translation_config["target_languages"]]
    chunks = build_chunks(segments, CHUNK_SIZE, CONTEXT_SIZE)
    total_chunks = len(chunks) * len(target_languages)
    completed_chunks = 0
    progress_lock = threading.Lock()
    content_type = str(translation_config.get("content_type", "general") or "general")
    custom_prompt = str(translation_config.get("custom_prompt", "") or "")

    def on_chunk_complete() -> None:
        nonlocal completed_chunks
        if progress_callback is None or total_chunks <= 0:
            return
        with progress_lock:
            completed_chunks += 1
            progress_callback(completed_chunks, total_chunks)

    translator = ChunkedTranslator(
        provider,
        content_type,
        custom_prompt,
        on_chunk_complete,
        database=database,
        llm_type=str(translation_config.get("llm_type", "openai-compatible")).strip(),
        model=str(translation_config["model"]).strip(),
    )
    for language in target_languages:
        last_error: Exception | None = None
        for _ in range(max_retries):
            try:
                translations[language] = translator.translate_language(segments, language)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            if isinstance(last_error, PipelineError):
                raise last_error
            raise PipelineError(str(last_error))
    return translations


def save_translations(context: TaskContext, payload: dict[str, list[str]]) -> Path:
    path = get_intermediate_path(context, "translations.json", create=True)
    _write_json(path, payload)
    return path


def load_translations(context: TaskContext) -> dict[str, list[str]]:
    path = get_intermediate_path(context, "translations.json")
    if not path.exists():
        raise PipelineError("缺少 translations.json，无法继续执行")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise PipelineError("translations.json 内容无效，无法继续执行")
    normalized: dict[str, list[str]] = {}
    for language, values in payload.items():
        if not isinstance(values, list):
            raise PipelineError("translations.json 内容无效，无法继续执行")
        normalized[str(language)] = [str(value) for value in values]
    return normalized


# ---------------------------------------------------------------------------
# Pipeline stage: render_srt (subtitle rendering)
# ---------------------------------------------------------------------------


def get_subtitle_target_dir(context: TaskContext) -> Path:
    file_config = context.config_snapshot["file"]
    source_path = Path(context.file_path)
    output_to_source_dir = bool(file_config.get("output_to_source_dir", True))
    if output_to_source_dir:
        return source_path.parent
    return Path(os.environ.get("SUBPIPELINE_OUTPUT_DIR", "/output"))


def expected_target_subtitle_paths(
    source_path: Path,
    file_config: dict[str, Any],
    subtitle_config: dict[str, Any],
    translation_config: dict[str, Any],
) -> list[Path]:
    # 枚举该视频预期的目标字幕路径;任一已存在则视为无需再翻译
    if not translation_config.get("enabled"):
        return []
    target_languages = [str(lang) for lang in translation_config.get("target_languages", [])]
    if not target_languages:
        return []
    template = str(subtitle_config["filename_template"])
    stem = source_path.stem
    if bool(file_config.get("output_to_source_dir", True)):
        target_dir = source_path.parent
    else:
        target_dir = Path(os.environ.get("SUBPIPELINE_OUTPUT_DIR", "/output"))
    bilingual = bool(subtitle_config.get("bilingual"))
    bilingual_mode = str(subtitle_config.get("bilingual_mode", "merge"))
    if bilingual and bilingual_mode == "merge":
        return [target_dir / template.format(stem=stem, lang="bilingual")]
    return [target_dir / template.format(stem=stem, lang=language) for language in target_languages]


def build_subtitle_tracks(context: TaskContext, translations: dict[str, list[str]]) -> list[dict[str, Any]]:
    subtitle_config = context.config_snapshot["subtitle"]
    source_path = Path(context.file_path)
    target_dir = get_subtitle_target_dir(context)
    template = str(subtitle_config["filename_template"])
    tracks: list[dict[str, Any]] = []
    if subtitle_config["bilingual"] and translations and subtitle_config["bilingual_mode"] == "merge":
        language = next(iter(translations))
        tracks.append(
            {
                "language": language,
                "path": target_dir / template.format(stem=source_path.stem, lang="bilingual"),
                "translated_lines": translations.get(language),
            }
        )
        return tracks
    if not translations:
        tracks.append(
            {
                "language": str(subtitle_config.get("source_language", "source")),
                "path": target_dir / template.format(stem=source_path.stem, lang="source"),
                "translated_lines": None,
            }
        )
        return tracks
    for language, translated_lines in translations.items():
        if subtitle_config["bilingual_mode"] == "merge":
            tracks.append(
                {
                    "language": language,
                    "path": target_dir / template.format(stem=source_path.stem, lang=language),
                    "translated_lines": translated_lines,
                }
            )
        else:
            # separate mode: only target language, replace source text
            tracks.append(
                {
                    "language": language,
                    "path": target_dir / template.format(stem=source_path.stem, lang=language),
                    "translated_lines": translated_lines,
                    "replace_source": True,
                }
            )
    return tracks


def render_srt(
    context: TaskContext,
    segments: list[dict[str, Any]],
    translations: dict[str, list[str]],
) -> list[str]:
    target_dir = get_subtitle_target_dir(context)
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    for track in build_subtitle_tracks(context, translations):
        output_path = Path(track["path"])
        content = build_srt_content(segments, track["translated_lines"], replace_source=track.get("replace_source", False))
        ensure_parent(output_path)
        output_path.write_text(content, encoding="utf-8")
        outputs.append(str(output_path))
    return outputs


def build_result_payload(
    context: TaskContext,
    audio_path: Path,
    subtitle_paths: list[str],
    translations: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "audio_path": str(audio_path),
        "subtitle_paths": subtitle_paths,
        "device": context.config_snapshot["whisper"]["device"],
        "translations": list(translations.keys()),
        "file_path_key": str(Path(context.file_path).expanduser().resolve()).lower(),
    }


def resolve_audio_path(context: TaskContext) -> Path:
    source_dir_audio = get_intermediate_path(context, "audio.wav")
    if source_dir_audio.exists():
        return source_dir_audio
    audio_format = str(context.config_snapshot["whisper"]["audio_format"]).strip().lower()
    fallback_audio = get_intermediate_path(context, f"audio.{audio_format}")
    if fallback_audio.exists():
        return fallback_audio
    raise PipelineError("缺少音频中间产物，无法继续执行")


# ---------------------------------------------------------------------------
# Pipeline stage: mux_subtitle
# ---------------------------------------------------------------------------


def _sanitize_language_code(value: str) -> str:
    normalized = value.replace("_", "-").strip()
    return normalized or "und"


def _resolve_mux_output_path(context: TaskContext) -> Path:
    mux_config = context.config_snapshot["mux"]
    source_path = Path(context.file_path)
    output_to_source_dir = bool(context.config_snapshot["file"].get("output_to_source_dir", True))
    target_dir = source_path.parent if output_to_source_dir else Path(os.environ.get("SUBPIPELINE_OUTPUT_DIR", "/output"))
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = str(mux_config["filename_template"]).format(stem=source_path.stem)
    output_path = target_dir / filename
    if output_path.suffix.lower() != ".mkv":
        output_path = output_path.with_suffix(".mkv")
    return output_path


def mux_subtitle(context: TaskContext, subtitle_paths: list[str]) -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise PipelineError("ffmpeg 未安装，无法执行字幕封装")
    source_path = Path(context.file_path)
    expected_languages: dict[str, str] = {}
    translations = load_translations(context) if context.config_snapshot["translation"]["enabled"] else {}
    for track in build_subtitle_tracks(context, translations):
        expected_languages[str(track["path"])] = _sanitize_language_code(str(track["language"]))
    command = [ffmpeg_path, "-y", "-i", str(source_path)]
    for subtitle_path in subtitle_paths:
        command.extend(["-i", subtitle_path])
    command.extend(["-map", "0:v", "-map", "0:a"])
    for index in range(len(subtitle_paths)):
        command.extend(["-map", str(index + 1)])
    command.extend(["-c", "copy", "-c:s", "srt"])
    for index, subtitle_path in enumerate(subtitle_paths):
        command.extend(
            [
                f"-metadata:s:s:{index}",
                f"language={expected_languages.get(subtitle_path, 'und')}",
            ]
        )
    output_path = _resolve_mux_output_path(context)
    command.append(str(output_path))
    _run_ffmpeg(command, f"字幕封装超时，FFmpeg 执行超过 {FFMPEG_TIMEOUT_SECONDS} 秒", "ffmpeg 字幕封装失败")
    return str(output_path)


# ---------------------------------------------------------------------------
# Resume feasibility
# ---------------------------------------------------------------------------


def _required_resume_files(resume_stage: str, context: TaskContext) -> list[tuple[str, Path]]:
    stage = normalize_stage_name(resume_stage)
    translation_enabled = bool(context.config_snapshot["translation"]["enabled"])
    audio_format = str(context.config_snapshot["whisper"]["audio_format"]).strip().lower()
    files: list[tuple[str, Path]] = []
    if stage in {"run_asr", "align_segments", "text_process", "translate", "subtitle_render", "output_finalize", "mux"}:
        files.append((f"audio.{audio_format}", get_intermediate_path(context, f"audio.{audio_format}")))
    if stage in {"align_segments", "text_process", "translate", "subtitle_render", "output_finalize", "mux"}:
        files.append(("asr_result.json", get_intermediate_path(context, "asr_result.json")))
    if stage in {"text_process", "translate", "subtitle_render", "output_finalize", "mux"}:
        files.append(("aligned_segments.json", get_intermediate_path(context, "aligned_segments.json")))
    if stage in {"translate", "subtitle_render", "output_finalize", "mux"}:
        files.append(("processed_segments.json", get_intermediate_path(context, "processed_segments.json")))
    if translation_enabled and stage in {"subtitle_render", "output_finalize", "mux"}:
        files.append(("translations.json", get_intermediate_path(context, "translations.json")))
    if stage in {"output_finalize", "mux"}:
        translations: dict[str, list[str]] = {}
        if translation_enabled:
            translations_path = get_intermediate_path(context, "translations.json")
            if translations_path.exists():
                payload = _read_json(translations_path)
                if isinstance(payload, dict):
                    translations = {
                        str(language): [str(value) for value in values]
                        for language, values in payload.items()
                        if isinstance(values, list)
                    }
        for track in build_subtitle_tracks(context, translations):
            files.append((Path(track["path"]).name, Path(track["path"])))
    if stage == "mux":
        files.append(("artifacts.json", context.work_dir / "artifacts.json"))
    return files


def _infer_resume_stage(current_stage: str, context: TaskContext) -> str:
    stage = normalize_stage_name(current_stage)
    audio_format = str(context.config_snapshot["whisper"]["audio_format"]).strip().lower()
    audio_exists = get_intermediate_path(context, f"audio.{audio_format}").exists()
    asr_exists = get_intermediate_path(context, "asr_result.json").exists()
    aligned_exists = get_intermediate_path(context, "aligned_segments.json").exists()
    processed_exists = get_intermediate_path(context, "processed_segments.json").exists()
    translations_exists = get_intermediate_path(context, "translations.json").exists()
    artifacts_exist = (context.work_dir / "artifacts.json").exists()

    if stage in {"run_asr", "align_segments", "text_process", "translate", "subtitle_render", "output_finalize", "mux"} and not audio_exists:
        return "extract_audio"
    if stage in {"align_segments", "text_process", "translate", "subtitle_render", "output_finalize", "mux"} and not asr_exists:
        return "run_asr"
    if stage in {"text_process", "translate", "subtitle_render", "output_finalize", "mux"} and not aligned_exists:
        return "align_segments"
    if stage in {"translate", "subtitle_render", "output_finalize", "mux"} and not processed_exists:
        return "text_process"
    if bool(context.config_snapshot["translation"]["enabled"]) and stage in {"subtitle_render", "output_finalize", "mux"} and not translations_exists:
        return "translate"
    if stage in {"output_finalize", "mux"} and not artifacts_exist and stage == "mux":
        return "output_finalize"
    return stage


def check_resume_feasibility(task: dict[str, Any]) -> dict[str, Any]:
    snapshot = task.get("config_snapshot")
    if not snapshot:
        return {"can_resume": False, "missing": ["config_snapshot"]}
    work_dir = Path(snapshot["processing"]["work_dir"]) / str(task["id"])
    context = TaskContext(
        task_id=int(task["id"]),
        file_path=str(task["file_path"]),
        config_snapshot=snapshot,
        work_dir=work_dir,
    )
    resume_stage = _infer_resume_stage(str(task["stage"]), context)
    missing: list[str] = []
    files = _required_resume_files(resume_stage, context)
    for name, path in files:
        if not path.exists():
            missing.append(name)
            continue
        if path.suffix.lower() == ".json":
            try:
                _read_json(path)
            except Exception:
                missing.append(name)
    return {"can_resume": not missing, "missing": missing, "resume_stage": resume_stage}
