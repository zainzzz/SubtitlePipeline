from __future__ import annotations

import logging
from copy import deepcopy
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def detect_device() -> str:
    """Auto-detect the best available device (cuda or cpu)."""
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            logger.info("检测到 CUDA 设备: %s，使用 GPU 加速", name)
            return "cuda"
    except ImportError:
        pass
    logger.info("未检测到 CUDA，使用 CPU 模式")
    return "cpu"


DEFAULT_CONFIG = {
    "file": {
        "input_dir": "/data",
        "output_to_source_dir": True,
        "allowed_extensions": [".mp4", ".mkv", ".mov", ".avi"],
        "scan_interval_seconds": 5,
        "min_size_mb": 128,
        "max_size_mb": 8192,
        "exclude_dirs": [],
        "input_dirs": [],
        "scan_enabled": True,
        "max_pending_tasks": 100,
    },
    "processing": {
        "max_retries": 1,
        "retry_mode": "restart",
        "keep_intermediates": False,
        "poll_interval_seconds": 2,
        "work_dir": "/config/work",
        # Pre-ASR resource check: refuse to start Whisper/Qwen if the GPU
        # doesn't have enough VRAM or work_dir doesn't have enough free disk.
        # Set to false for power-user / GPU-sharing setups where they want
        # the worker to try anyway.
        "pre_asr_resource_check": True,
        # Headroom percentage added on top of the estimated requirement,
        # so other processes (or temp spikes) don't push us into OOM.
        "resource_headroom_pct": 20,
    },
    "whisper": {
        "provider": "whisperx",
        "model_name": "whisperx-small",
        "device": "auto",
        "audio_format": "wav",
        "sample_rate": 16000,
        # 通用配置（提升到顶层）
        "beam_size": 5,
        "vad_filter": True,
        "vad_threshold": 0.5,
        # 对齐 Provider 选择
        "align_provider": "auto",  # auto | whisperx | qwen-forced | none
        # 高级配置（Provider 特定）
        "advanced": {
            "whisperx_align_extend": 2,
            "whisperx_compute_type": "auto",
            "faster_whisper_word_timestamps": False,
            "faster_whisper_compute_type": "auto",
            "anime_whisper_enhance_dialogue": True,
            "anime_whisper_dtype": "auto",
            "qwen_temperature": 0.0,
            "qwen_dtype": "auto",
            "qwen_max_inference_batch_size": 32,
            "qwen_max_new_tokens": 256,
        },
    },
    "translation": {
        "enabled": True,
        "target_languages": ["zh"],
        "max_retries": 2,
        "timeout_seconds": 30,
        "llm_type": "openai-chat",
        "api_base_url": "https://api.openai.com",
        "api_key": "",
        "model": "gpt-4o-mini",
        "content_type": "general",
        "custom_prompt": "",
        # LLM sampling parameters — exposed so users can tune quality / cost.
        "temperature": 0.3,
        "max_tokens": 8192,
        "frequency_penalty": 1.2,
        "presence_penalty": 0.8,
        # Per-request HTTP-layer retry count for transient 5xx / connection errors.
        # Applied via the OpenAI client constructor; non-OpenAI providers rely on
        # their own urllib-based clients (no automatic retry today).
        "http_max_retries": 3,
    },
    "subtitle": {
        "bilingual": True,
        "bilingual_mode": "merge",
        "filename_template": "{stem}.forced.{lang}.srt",
        "source_language": "auto",
    },
    "mux": {
        "enabled": False,
        "filename_template": "{stem}.subbed.mkv",
    },
    "logging": {
        "level": "INFO",
    },
    "notification": {
        "webhook_enabled": False,
        "webhook_type": "jellyfin",  # jellyfin | emby | plex | generic
        "webhook_url": "",
        "webhook_token": "",  # Jellyfin/Emby API key or Plex token
        "webhook_library_id": "",  # comma-separated library IDs (Jellyfin/Emby) or section IDs (Plex)
        "trigger_on_subtitle_change": True,  # also fire webhook when user edits / re-renders subtitles after a task is done
        "subtitle_change_debounce_seconds": 5,  # coalesce rapid edits into one webhook
    },
    "schedule": {
        "enabled": False,
        "start_time": "00:00",  # HH:MM, 24h format
        "end_time": "23:59",
        "timezone": "Asia/Shanghai",
    },
    "audio": {
        "prefer_languages": [],  # e.g. ["jpn", "eng"] — process each track in order
        "track_selection_mode": "first",  # first | prefer | all
    },
    "quality": {
        "enabled": True,
        "min_avg_confidence": 0.6,        # ASR mean confidence floor; below this triggers a warning
        "min_segment_duration": 0.8,      # segments shorter than this (s) are flagged
        "max_segment_duration": 12.0,     # segments longer than this (s) are flagged
        "max_repeat_segments": 3,         # N consecutive same-text segments → "ASR hallucination" error
        "min_translation_char_ratio": 0.2, # translated/source char ratio lower bound
        "max_translation_char_ratio": 3.0, # translated/source char ratio upper bound
        "suspect_score_threshold": 80,    # tasks with score below this are listed as "可疑字幕"
    },
}

SYSTEM_LEVEL_FIELDS = {
    ("whisper", "model_name"),
}

RESULT_AFFECTING_GROUPS = {"file", "processing", "whisper", "translation", "subtitle", "mux", "audio"}
STAGE_SEQUENCE = [
    "extract_audio",
    "run_asr",
    "align_segments",
    "text_process",
    "translate",
    "subtitle_render",
    "output_finalize",
    "mux",
]


def copy_default_config() -> dict:
    return deepcopy(DEFAULT_CONFIG)
