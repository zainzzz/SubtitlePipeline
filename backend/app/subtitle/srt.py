"""Pure SRT (SubRip) format helpers extracted from pipeline.py.

This module owns:
  * ``format_srt_time`` — seconds → ``HH:MM:SS,mmm`` timestamp formatting
  * ``build_srt_content`` — segment list → full SRT document string
  * ``write_stage_artifacts`` / ``read_stage_artifacts`` — JSON stage I/O

The functions intentionally accept plain dicts / ``TaskContext``-shaped objects
so that the subtitle module has no hard runtime dependency on ``app.pipeline``
(avoids a circular import).  ``TaskContext`` is only referenced inside
``TYPE_CHECKING`` for static analysis.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — import only for type checkers
    from app.pipeline import TaskContext

logger = logging.getLogger(__name__)


def format_srt_time(seconds: float) -> str:
    """Convert ``seconds`` (float) to SubRip timestamp ``HH:MM:SS,mmm``.

    Negative values are clamped to zero.  Fractional milliseconds are rounded
    to the nearest whole millisecond.
    """
    if seconds < 0:
        seconds = 0.0
    total_milliseconds = int(round(seconds * 1000))
    milliseconds = total_milliseconds % 1000
    total_seconds = total_milliseconds // 1000
    minutes, seconds_value = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds_value:02},{milliseconds:03}"


def build_srt_content(
    segments: list[dict[str, Any]],
    translated_lines: list[str] | None,
    replace_source: bool = False,
) -> str:
    """Render a list of segment dicts into a full SRT document.

    Parameters mirror the historical pipeline signature:

    * ``segments`` — ``[{start, end, text}, ...]``
    * ``translated_lines`` — optional parallel translation list; when present
      each block becomes bilingual (source + translation) unless
      ``replace_source`` is true.
    * ``replace_source`` — when true and translations exist, the source text is
      replaced by the translation (used for ``bilingual_mode = separate``).
    """
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        if replace_source and translated_lines:
            lines = [translated_lines[index - 1]]
        else:
            lines = [segment["text"]]
            if translated_lines:
                lines.append(translated_lines[index - 1])
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_srt_time(float(segment['start']))} --> {format_srt_time(float(segment['end']))}",
                    *lines,
                ]
            )
        )
    return "\n\n".join(blocks) + "\n"


def write_stage_artifacts(context: "TaskContext", payload: dict[str, Any]) -> None:
    """Persist ``payload`` as ``artifacts.json`` inside the task work dir."""
    artifact_path = context.work_dir / "artifacts.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_stage_artifacts(context: "TaskContext") -> dict[str, Any]:
    """Read and validate ``artifacts.json`` from the task work dir.

    Raises ``PipelineError`` (imported lazily to avoid the circular import at
    module load time) when the file is missing or malformed.
    """
    # Late import — keeps subtitle.srt a leaf module at import time.
    from app.pipeline import PipelineError

    artifact_path = context.work_dir / "artifacts.json"
    if not artifact_path.exists():
        raise PipelineError("缺少 artifacts.json，无法继续执行")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PipelineError("artifacts.json 内容无效，无法继续执行")
    return payload
