"""Lightweight post-run quality checks for finished subtitle tasks.

Goal: catch the common "字幕质量有问题" cases (low ASR confidence, suspiciously
short or long segments, hallucinated repetition, empty translations, source/translation
character ratio anomalies) so the UI can surface a "可疑字幕" list instead of
making the user scrub through every output file.

Design constraints:
- Pure function: no DB / FS / network access. The caller (the worker) feeds in
  the in-memory segments and the translations dict.
- No ML — only statistics. (Confidence comes from the ASR provider; we just
  aggregate it.)
- Graceful degradation: if a field is missing (e.g. older ASR provider that
  doesn't emit per-word confidence) we skip that check rather than fail.
- Score: 100 - penalty, clamped to [0, 100]. A "suspect" task is one with
  score < 80 OR any severity == "error" issue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


# Issue codes — keep these stable; the UI / tests depend on them.
ISSUE_LOW_CONFIDENCE = "low_confidence"
ISSUE_SHORT_SEGMENT = "segment_too_short"
ISSUE_LONG_SEGMENT = "segment_too_long"
ISSUE_REPEATED_SEGMENT = "repeated_segment"
ISSUE_EMPTY_TRANSLATION = "empty_translation"
ISSUE_TRANSLATION_RATIO = "translation_ratio_anomaly"
ISSUE_TINY_SEGMENT_COUNT = "tiny_segment_count"


@dataclass
class QualityIssue:
    """A single flagged condition.

    Attributes:
        code: Stable identifier (one of ISSUE_*). UI / API consumers can
            switch on this without parsing free-form messages.
        severity: "warning" (mild) or "error" (likely needs user attention).
        message: Human-readable description (zh-CN).
        segment_index: When the issue is tied to one or more specific segments,
            this is the index in the source/aligned segment list. None for
            aggregate issues (e.g. overall low confidence).
        detail: Optional structured payload (e.g. {"count": 3, "ratio": 0.12}).
    """

    code: str
    severity: str  # "warning" | "error"
    message: str
    segment_index: int | None = None
    detail: dict[str, Any] | None = None


@dataclass
class QualityReport:
    """Aggregate quality result for one task.

    Attributes:
        score: 0-100. Higher = healthier. Computed as 100 minus total penalty,
            clamped. NULL/None means the check was skipped (no input).
        issues: All detected issues, in detection order. May be empty.
        suspect_segment_ids: Set of segment indices (as ints) the user should
            jump to in the editor. UI uses this to scroll to the first problem.
        is_suspect: True if score < 80 OR any issue has severity == "error".
            The UI uses this to put the task in the "可疑字幕" list.
        summary: Short one-line human summary (zh-CN) for compact display.
    """

    score: int | None
    issues: list[QualityIssue] = field(default_factory=list)
    suspect_segment_ids: list[int] = field(default_factory=list)
    is_suspect: bool = False
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "issues": [asdict(issue) for issue in self.issues],
            "suspect_segment_ids": list(self.suspect_segment_ids),
            "is_suspect": self.is_suspect,
            "summary": self.summary,
        }


def _segment_duration(segment: dict[str, Any]) -> float:
    try:
        return max(0.0, float(segment.get("end", 0.0)) - float(segment.get("start", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _segment_text(segment: dict[str, Any]) -> str:
    return str(segment.get("text", "") or "").strip()


def _average_confidence(segments: list[dict[str, Any]]) -> float | None:
    """Return mean of any per-segment / per-word confidence values, or None if
    no confidence data is present. Confidence is expected on a 0-1 scale; values
    outside that range are clamped."""
    values: list[float] = []
    for segment in segments:
        # WhisperX exposes segment-level "score" or word-level "score" on words
        if "score" in segment:
            try:
                values.append(max(0.0, min(1.0, float(segment["score"]))))
                continue
            except (TypeError, ValueError):
                pass
        words = segment.get("words")
        if isinstance(words, list):
            for word in words:
                if isinstance(word, dict) and "score" in word:
                    try:
                        values.append(max(0.0, min(1.0, float(word["score"]))))
                    except (TypeError, ValueError):
                        pass
    if not values:
        return None
    return sum(values) / len(values)


def _detect_repetition(
    segments: list[dict[str, Any]],
    max_repeat: int,
) -> list[QualityIssue]:
    """Flag runs of >= max_repeat consecutive segments with the same text.
    Common Whisper hallucination when the audio is silent."""
    issues: list[QualityIssue] = []
    if max_repeat < 2:
        return issues
    run_start = 0
    run_text = _segment_text(segments[0]) if segments else ""
    for index in range(1, len(segments) + 1):
        current_text = _segment_text(segments[index]) if index < len(segments) else None
        if current_text == run_text and run_text:
            continue
        run_length = index - run_start
        if run_length >= max_repeat:
            issues.append(
                QualityIssue(
                    code=ISSUE_REPEATED_SEGMENT,
                    severity="error",
                    message=f"连续 {run_length} 段重复文本（疑似 ASR 幻觉）",
                    segment_index=run_start,
                    detail={"run_length": run_length, "text": run_text[:60]},
                )
            )
        run_start = index
        run_text = current_text or ""
    return issues


def _detect_duration_anomalies(
    segments: list[dict[str, Any]],
    min_duration: float,
    max_duration: float,
) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for index, segment in enumerate(segments):
        duration = _segment_duration(segment)
        if duration <= 0:
            continue
        if duration < min_duration:
            issues.append(
                QualityIssue(
                    code=ISSUE_SHORT_SEGMENT,
                    severity="warning",
                    message=f"段时长仅 {duration:.1f}s（阈值 {min_duration:.1f}s）",
                    segment_index=index,
                    detail={"duration": duration},
                )
            )
        elif duration > max_duration:
            issues.append(
                QualityIssue(
                    code=ISSUE_LONG_SEGMENT,
                    severity="warning",
                    message=f"段时长 {duration:.1f}s（阈值 {max_duration:.1f}s）",
                    segment_index=index,
                    detail={"duration": duration},
                )
            )
    return issues


def _detect_translation_issues(
    aligned_segments: list[dict[str, Any]],
    translations: dict[str, list[str]],
    min_ratio: float,
    max_ratio: float,
) -> list[QualityIssue]:
    """Two checks: empty translations, and source/translation character ratio."""
    issues: list[QualityIssue] = []
    if not translations:
        return issues
    for lang, lines in translations.items():
        if len(lines) != len(aligned_segments):
            # Schema mismatch — not a quality issue, the previous stage would
            # have raised. Skip silently.
            continue
        for index, (segment, translated) in enumerate(zip(aligned_segments, lines)):
            stripped = (translated or "").strip()
            if not stripped:
                issues.append(
                    QualityIssue(
                        code=ISSUE_EMPTY_TRANSLATION,
                        severity="warning",
                        message=f"[{lang}] 第 {index + 1} 段翻译为空",
                        segment_index=index,
                        detail={"language": lang},
                    )
                )
                continue
            source_text = _segment_text(segment)
            if not source_text:
                continue
            source_len = len(source_text)
            translated_len = len(stripped)
            if source_len == 0:
                continue
            ratio = translated_len / source_len
            if ratio < min_ratio or ratio > max_ratio:
                issues.append(
                    QualityIssue(
                        code=ISSUE_TRANSLATION_RATIO,
                        severity="warning",
                        message=f"[{lang}] 第 {index + 1} 段翻译长度比异常 ({ratio:.2f}, 阈值 {min_ratio}-{max_ratio})",
                        segment_index=index,
                        detail={"language": lang, "ratio": round(ratio, 2), "source_len": source_len, "translated_len": translated_len},
                    )
                )
    return issues


def check_quality(
    aligned_segments: list[dict[str, Any]] | None,
    translations: dict[str, list[str]] | None = None,
    *,
    asr_segments: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> QualityReport:
    """Run the full quality check battery on one finished task.

    Args:
        aligned_segments: Aligned/cleaned segments (start/end/text). Required.
        translations: Optional dict of language -> list of translated strings,
            same length as aligned_segments. Empty / None disables translation checks.
        asr_segments: Optional raw ASR segments (with confidence). If absent,
            confidence check is skipped.
        config: App config dict; the `quality` block is read for thresholds.
            Missing block → module defaults.

    Returns:
        QualityReport. Never raises.
    """
    if not aligned_segments:
        return QualityReport(
            score=None,
            issues=[],
            suspect_segment_ids=[],
            is_suspect=False,
            summary="无可用段落",
        )

    cfg = (config or {}).get("quality", {}) or {}
    min_conf = float(cfg.get("min_avg_confidence", 0.6))
    min_dur = float(cfg.get("min_segment_duration", 0.8))
    max_dur = float(cfg.get("max_segment_duration", 12.0))
    max_repeat = int(cfg.get("max_repeat_segments", 3))
    min_ratio = float(cfg.get("min_translation_char_ratio", 0.2))
    max_ratio = float(cfg.get("max_translation_char_ratio", 3.0))
    suspect_threshold = int(cfg.get("suspect_score_threshold", 80))

    issues: list[QualityIssue] = []

    # 1. Aggregate confidence (use raw ASR segments if available, else aligned).
    confidence_source = asr_segments if asr_segments else aligned_segments
    avg_conf = _average_confidence(confidence_source)
    if avg_conf is not None and avg_conf < min_conf:
        issues.append(
            QualityIssue(
                code=ISSUE_LOW_CONFIDENCE,
                severity="error" if avg_conf < min_conf - 0.2 else "warning",
                message=f"ASR 平均置信度 {avg_conf:.2f} 低于阈值 {min_conf:.2f}",
                segment_index=None,
                detail={"avg_confidence": round(avg_conf, 3), "min_required": min_conf},
            )
        )

    # 2. Per-segment duration anomalies.
    issues.extend(_detect_duration_anomalies(aligned_segments, min_dur, max_dur))

    # 3. Repetition runs.
    issues.extend(_detect_repetition(aligned_segments, max_repeat))

    # 4. Translation issues (only if translations are present).
    issues.extend(_detect_translation_issues(
        aligned_segments, translations or {}, min_ratio, max_ratio,
    ))

    # 5. Tiny segment count — too few segments means audio wasn't really decoded.
    if len(aligned_segments) < 3:
        issues.append(
            QualityIssue(
                code=ISSUE_TINY_SEGMENT_COUNT,
                severity="warning",
                message=f"仅识别出 {len(aligned_segments)} 段（通常应该更多）",
                segment_index=None,
                detail={"count": len(aligned_segments)},
            )
        )

    # Compute score: 100 - sum(weighted penalties), clamped.
    penalty = 0
    for issue in issues:
        if issue.severity == "error":
            penalty += 25
        else:
            penalty += 8
    score = max(0, min(100, 100 - penalty))

    suspect_ids = sorted({i for i in (issue.segment_index for issue in issues) if i is not None})
    is_suspect = score < suspect_threshold or any(issue.severity == "error" for issue in issues)

    if not issues:
        summary = f"质量良好 (score {score})"
    else:
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = len(issues) - error_count
        parts = []
        if error_count:
            parts.append(f"{error_count} 个错误")
        if warning_count:
            parts.append(f"{warning_count} 个警告")
        summary = f"score {score}，{'，'.join(parts)}"

    return QualityReport(
        score=score,
        issues=issues,
        suspect_segment_ids=suspect_ids,
        is_suspect=is_suspect,
        summary=summary,
    )


def is_suspect_report(report: QualityReport | dict[str, Any] | None) -> bool:
    """Convenience predicate for the UI / API layer."""
    if report is None:
        return False
    if isinstance(report, dict):
        return bool(report.get("is_suspect", False))
    return report.is_suspect
