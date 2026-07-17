"""Chunked LLM translation extracted from pipeline.py.

This module owns everything related to LLM-driven translation:

* Constants: ``CHUNK_SIZE``, ``CONTEXT_SIZE``, retry counters,
  ``TRANSLATION_PRESETS``, ``FORMAT_INSTRUCTION``.
* Value types: ``ChunkLine``, ``TranslationChunk``, ``ParseResult``,
  ``TranslationProvider``.
* Exception: ``TranslationRateLimitError`` (subclasses ``PipelineError``).
* Helpers: ``build_chunks``, ``build_chunk_user_message``,
  ``parse_numbered_lines``, ``parse_chunk_output``, ``strip_think`` …
* Classes: ``ChunkedTranslator``, ``LLMTranslationProvider``.
* Debug entrypoint: ``debug_translation_request``.

``PipelineError`` is imported from ``app.pipeline``; this creates a benign
circular dependency that Python resolves because ``pipeline.py`` defines
``PipelineError`` before it imports this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from app.llm import LLMError, LLMMessage, LLMRateLimitError, create_llm_client

# ``PipelineError`` is defined early in ``app.pipeline`` (well before the
# ``from .llm.translation import ...`` re-export block), so the partially
# initialised module exposes it when this line runs.
from app.pipeline import PipelineError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

CHUNK_SIZE = 15
CONTEXT_SIZE = 5
MAX_CHUNK_WORKERS = 1
MAX_CHUNK_RETRIES = 5
MAX_PARTIAL_RETRIES = 3

TRANSLATION_PRESETS = {
    "general": "You are a professional subtitle translator. Keep the translation natural, accurate, concise, and easy to read on screen.",
    "movie": "You are a professional film and TV subtitle translator. Keep dialogue natural and conversational, preserve character voice, and localize idioms smoothly.",
    "documentary": "You are a documentary subtitle translator. Keep the tone clear, informative, and slightly formal. Preserve important terms accurately.",
    "anime": "You are an anime subtitle translator. Preserve character tone, emotional rhythm, and genre-specific expressions while keeping subtitles natural.",
    "tech_talk": "You are a technical talk subtitle translator. Keep terminology precise, preserve key English technical terms when appropriate, and maintain logical clarity.",
    "variety_show": "You are a variety show subtitle translator. Keep the tone lively, witty, and audience-friendly while preserving humor and timing.",
    "news": "You are a news subtitle translator. Keep the tone formal, objective, and consistent with standard naming conventions for people and places.",
}

FORMAT_INSTRUCTION = (
    "Translate the numbered lines into {target_language}. "
    'Return exactly one line per item using the format "编号|译文". '
    "Only translate the [翻译] section when it exists. "
    "The [上文] and [下文] sections are context only and must not be translated. "
    "If no [翻译] section exists, translate all numbered lines. "
    "Keep the same ids and line count as the content to translate. "
    "Do not output markdown, JSON, code fences, explanations, or any extra text."
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TranslationRateLimitError(PipelineError):
    pass


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkLine:
    index: int
    text: str


@dataclass(frozen=True)
class TranslationChunk:
    start_index: int
    context_before: list[ChunkLine]
    main_segments: list[ChunkLine]
    context_after: list[ChunkLine]
    use_sections: bool


class TranslationProvider:
    def translate_batch(self, texts: list[str], target_language: str) -> list[str]:
        raise NotImplementedError


@dataclass
class ParseResult:
    """Result of parsing numbered translation lines.

    ``matched`` maps expected line index -> translated text for all lines
    that were successfully parsed in order.  ``first_failed_position``
    is the 0-based position in ``expected_ids`` where the first gap or
    mismatch occurred (``None`` means everything matched).
    """

    matched: dict[int, str]
    first_failed_position: int | None


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------


def build_chunks(
    segments: list[dict[str, Any]],
    chunk_size: int = CHUNK_SIZE,
    context_size: int = CONTEXT_SIZE,
) -> list[TranslationChunk]:
    if not segments:
        return []
    total = len(segments)
    use_sections = total > chunk_size
    chunks: list[TranslationChunk] = []
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        context_before = [
            ChunkLine(index=index, text=str(segments[index]["text"]))
            for index in range(max(0, start - context_size), start)
        ]
        main_segments = [ChunkLine(index=index, text=str(segments[index]["text"])) for index in range(start, end)]
        context_after = [
            ChunkLine(index=index, text=str(segments[index]["text"]))
            for index in range(end, min(total, end + context_size))
        ]
        chunks.append(
            TranslationChunk(
                start_index=start,
                context_before=context_before,
                main_segments=main_segments,
                context_after=context_after,
                use_sections=use_sections,
            )
        )
    return chunks


def build_chunk_user_message(chunk: TranslationChunk) -> str:
    def format_lines(lines: list[ChunkLine]) -> str:
        return "\n".join(f"{line.index}|{line.text}" for line in lines)

    if not chunk.use_sections:
        return format_lines(chunk.main_segments)
    parts: list[str] = []
    if chunk.context_before:
        parts.extend(["[上文]", format_lines(chunk.context_before)])
    parts.extend(["[翻译]", format_lines(chunk.main_segments)])
    if chunk.context_after:
        parts.extend(["[下文]", format_lines(chunk.context_after)])
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Output parsing helpers
# ---------------------------------------------------------------------------


def strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def strip_number_prefix(line: str) -> str:
    """Strip number prefix from translation line.

    Handles formats:
    - 编号|译文 (half-width pipe)
    - 编号｜译文 (full-width pipe)
    - 编号||译文 (double pipe)
    - 编号. 译文
    - 编号: 译文 / 编号：译文
    - 编号译文 (bare number, last resort)
    - |译文 (bare leading pipe, no number)
    """
    # Try numbered formats with delimiters first
    stripped = re.sub(r"^\s*\d+[|｜.:：]+\s*", "", line, count=1)
    if stripped != line:
        # Also strip any remaining leading pipes
        return stripped.lstrip("|｜").strip()

    # Bare leading pipe(s) with no number prefix
    if line.strip().startswith("|") or line.strip().startswith("｜"):
        return line.strip().lstrip("|｜").strip()

    # Last resort: bare number at start (e.g., "9怎么样？")
    stripped = re.sub(r"^\s*\d+", "", line, count=1)
    return stripped.strip()


def parse_numbered_lines_ordered(
    raw_output: str,
    expected_ids: list[int],
) -> ParseResult:
    """Parse ``编号|译文`` lines and identify the first failure point.

    Lines are scanned in document order.  The parser collects every valid
    match whose index belongs to ``expected_ids``.  After collecting, we
    walk ``expected_ids`` sequentially; the first id that is missing marks
    the failure boundary – every id *before* it is considered valid, every
    id from that point on is considered failed (even if some later ids
    happened to match) so that the caller can retry a contiguous suffix.
    """
    matches: dict[int, str] = {}
    expected = set(expected_ids)
    for line in strip_code_fence(raw_output).splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        # Try to match "编号|译文" or "编号｜译文" format (half-width or full-width pipe)
        if "|" in normalized or "｜" in normalized:
            # Replace full-width pipe with half-width for uniform processing
            normalized_pipe = normalized.replace("｜", "|")
            prefix, value = normalized_pipe.split("|", 1)
            if prefix.strip().isdigit():
                index = int(prefix.strip())
                if index in expected and index not in matches:
                    cleaned_value = value.strip().lstrip("|｜").strip()
                    matches[index] = cleaned_value
                continue
        # Also accept "编号. 译文" or "编号: 译文" as fallback
        m = re.match(r"^\s*(\d+)\s*[.:：]\s*(.+)$", normalized)
        if m:
            index = int(m.group(1))
            if index in expected and index not in matches:
                matches[index] = m.group(2).strip()
            continue
        # Last resort: bare number prefix "编号译文" (no delimiter)
        m = re.match(r"^\s*(\d+)(\S.*)$", normalized)
        if m:
            index = int(m.group(1))
            if index in expected and index not in matches:
                matches[index] = m.group(2).strip()
            continue
        # Last resort: bare number prefix "编号译文" (LLM forgot the separator)
        m = re.match(r"^\s*(\d+)(\S.*)$", normalized)
        if m:
            index = int(m.group(1))
            if index in expected and index not in matches:
                matches[index] = m.group(2).strip()

    # Walk expected_ids to find the first gap
    first_failed: int | None = None
    for position, eid in enumerate(expected_ids):
        if eid not in matches:
            first_failed = position
            break

    if first_failed is not None:
        # Only keep the contiguous prefix (discard any sporadic matches after the gap)
        valid_ids = set(expected_ids[:first_failed])
        matches = {k: v for k, v in matches.items() if k in valid_ids}

    return ParseResult(matched=matches, first_failed_position=first_failed)


def parse_numbered_lines(
    raw_output: str,
    expected_ids: list[int],
    source_texts: list[str] | None = None,
) -> list[str] | None:
    result = parse_numbered_lines_ordered(raw_output, expected_ids)
    if result.first_failed_position is None:
        return [result.matched[eid] for eid in expected_ids]
    if source_texts is not None and expected_ids:
        matched_ratio = len(result.matched) / len(expected_ids)
        if matched_ratio >= 0.8:
            missing = [eid for eid in expected_ids if eid not in result.matched]
            logger.warning("分块翻译部分匹配，缺失编号将回退原文: %s", missing)
            return [result.matched.get(eid, source_texts[pos]) for pos, eid in enumerate(expected_ids)]
    return None


def strip_think(content: str) -> str:
    """去掉思考模型(如 MiniMax-M2 / DeepSeek-R1)的 <think>...</think> 块,避免污染解析。"""
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()


def parse_chunk_output(
    raw_output: str,
    expected_ids: list[int],
    source_texts: list[str],
    json_array_parser=None,
) -> list[str]:
    raw_output = strip_think(raw_output)
    numbered = parse_numbered_lines(raw_output, expected_ids, source_texts)
    if numbered is not None:
        return numbered
    if json_array_parser is not None:
        try:
            parsed = json_array_parser(raw_output)
        except PipelineError:
            parsed = None
        if isinstance(parsed, list) and len(parsed) == len(expected_ids):
            return [str(item).strip() for item in parsed]
    lines = [strip_number_prefix(line) for line in strip_code_fence(raw_output).splitlines() if line.strip()]
    if len(lines) == len(expected_ids):
        return lines
    # Last resort: if lines are close (within 20%), pad or truncate
    if lines and abs(len(lines) - len(expected_ids)) <= max(1, len(expected_ids) // 5):
        logger.warning("翻译结果行数 %d != 期望 %d，尝试对齐", len(lines), len(expected_ids))
        if len(lines) > len(expected_ids):
            return lines[:len(expected_ids)]
        return lines + [source_texts[i] for i in range(len(lines), len(expected_ids))]
    raise PipelineError(
        f"翻译返回结果无法解析 (得到 {len(lines)} 行, 期望 {len(expected_ids)} 行)"
    )


def _translation_cache_key(
    chunk: TranslationChunk,
    target_language: str,
    llm_type: str,
    model: str,
    content_type: str,
    custom_prompt: str,
) -> tuple[str, str, str, str, str, str]:
    source_text = "\n".join(line.text for line in chunk.main_segments)
    source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    custom_prompt_hash = hashlib.sha256((custom_prompt or "").encode("utf-8")).hexdigest()[:16]
    return (source_hash, target_language, llm_type, model, content_type, custom_prompt_hash)


# ---------------------------------------------------------------------------
# Chunked translator
# ---------------------------------------------------------------------------


class ChunkedTranslator:
    def __init__(
        self,
        provider: TranslationProvider,
        content_type: str,
        custom_prompt: str,
        on_chunk_complete=None,
        database: Any = None,
        llm_type: str = "",
        model: str = "",
    ):
        self.provider = provider
        self.content_type = content_type
        self.custom_prompt = custom_prompt
        self.on_chunk_complete = on_chunk_complete
        self.database = database
        self.llm_type = llm_type
        self.model = model
        self.pause_event = threading.Event()
        self.pause_event.set()

    def translate_language(self, segments: list[dict[str, Any]], target_language: str) -> list[str]:
        chunks = build_chunks(segments, CHUNK_SIZE, CONTEXT_SIZE)
        if not chunks:
            return []
        merged: list[str | None] = [None] * len(segments)
        pending = list(chunks)
        attempts = {chunk.start_index: 0 for chunk in chunks}
        max_workers = MAX_CHUNK_WORKERS
        while pending:
            current_batch = pending[:max_workers]
            pending = pending[max_workers:]
            rate_limited: list[TranslationChunk] = []
            max_wait = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(self._translate_chunk, chunk, target_language): chunk
                    for chunk in current_batch
                }
                for future in as_completed(futures):
                    chunk = futures[future]
                    try:
                        translated_lines = future.result()
                    except TranslationRateLimitError as exc:
                        attempts[chunk.start_index] += 1
                        if attempts[chunk.start_index] > MAX_CHUNK_RETRIES:
                            # Preserve TranslationRateLimitError type so callers can
                            # isinstance-check and apply rate-limit-specific backoff.
                            raise TranslationRateLimitError(f"分块翻译多次触发限流: {exc}") from exc
                        rate_limited.append(chunk)
                        max_wait = max(max_wait, 2 ** (attempts[chunk.start_index] - 1))
                    except PipelineError:
                        # Already a PipelineError (or subclass like
                        # TranslationRateLimitError) — re-raise as-is to preserve
                        # the original exception type for isinstance checks.
                        raise
                    except Exception as exc:
                        raise PipelineError(str(exc)) from exc
                    else:
                        for position, line in enumerate(chunk.main_segments):
                            merged[line.index] = translated_lines[position]
                        if self.on_chunk_complete is not None:
                            self.on_chunk_complete()
            if rate_limited:
                self.pause_event.clear()
                time.sleep(max_wait)
                self.pause_event.set()
                if max_workers > 1:
                    max_workers -= 1
                pending = rate_limited + pending
        if any(item is None for item in merged):
            raise PipelineError("分块翻译结果不完整")
        return [item or "" for item in merged]

    def _translate_chunk(self, chunk: TranslationChunk, target_language: str) -> list[str]:
        self.pause_event.wait()
        cacheable = self.database is not None and isinstance(self.provider, LLMTranslationProvider)
        cache_key = (
            _translation_cache_key(
                chunk, target_language, self.llm_type, self.model, self.content_type, self.custom_prompt
            )
            if cacheable
            else None
        )
        if cache_key is not None:
            cached = self.database.get_translation_cache(*cache_key)
            if cached is not None:
                return cached
        if isinstance(self.provider, LLMTranslationProvider):
            translated = self.provider.translate_chunk(chunk, target_language, self.content_type, self.custom_prompt)
        else:
            translated = self.provider.translate_batch([line.text for line in chunk.main_segments], target_language)
        if cache_key is not None:
            self.database.set_translation_cache(*cache_key, translated)
        return translated


# ---------------------------------------------------------------------------
# LLM translation provider
# ---------------------------------------------------------------------------


class LLMTranslationProvider(TranslationProvider):
    def __init__(self, llm_type: str, api_base_url: str, api_key: str, model: str, timeout_seconds: int):
        self.llm_type = llm_type
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.client = create_llm_client(
            llm_type=self.llm_type,
            api_base_url=self.api_base_url,
            api_key=self.api_key,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
        )

    def translate_batch(self, texts: list[str], target_language: str) -> list[str]:
        chunk = TranslationChunk(
            start_index=0,
            context_before=[],
            main_segments=[ChunkLine(index=index, text=text) for index, text in enumerate(texts)],
            context_after=[],
            use_sections=False,
        )
        return self.translate_chunk(chunk, target_language, "general", "")

    def translate_chunk(
        self,
        chunk: TranslationChunk,
        target_language: str,
        content_type: str,
        custom_prompt: str,
    ) -> list[str]:
        texts = [line.text for line in chunk.main_segments]
        if not texts:
            return []
        prompt = self._build_prompt(target_language, content_type, custom_prompt)

        # Partial-retry loop: keep valid prefix, only retry the tail
        results: list[str | None] = [None] * len(texts)
        remaining_segments = list(chunk.main_segments)
        remaining_context_before = list(chunk.context_before)

        for attempt in range(MAX_PARTIAL_RETRIES + 1):
            if not remaining_segments:
                break

            retry_chunk = TranslationChunk(
                start_index=remaining_segments[0].index,
                context_before=remaining_context_before,
                main_segments=remaining_segments,
                context_after=chunk.context_after,
                use_sections=chunk.use_sections or len(remaining_context_before) > 0,
            )
            content = self._request_translation(prompt, build_chunk_user_message(retry_chunk))
            remaining_ids = [line.index for line in remaining_segments]
            remaining_texts = [line.text for line in remaining_segments]

            # Try full parse first
            try:
                parsed = parse_chunk_output(
                    content, remaining_ids, remaining_texts, self._parse_json_array_content,
                )
                # Full success — fill in all remaining results
                for seg, translated in zip(remaining_segments, parsed):
                    pos = next(i for i, s in enumerate(chunk.main_segments) if s.index == seg.index)
                    results[pos] = translated
                remaining_segments = []
                break
            except PipelineError:
                pass

            # Full parse failed — try ordered partial parse
            pr = parse_numbered_lines_ordered(content, remaining_ids)
            if pr.matched:
                for seg_idx, translated in pr.matched.items():
                    pos = next(i for i, s in enumerate(chunk.main_segments) if s.index == seg_idx)
                    results[pos] = translated

                if pr.first_failed_position is not None:
                    # Keep matched prefix, retry from the failure point
                    kept = remaining_segments[:pr.first_failed_position]
                    remaining_context_before = kept[-CONTEXT_SIZE:] if kept else remaining_context_before
                    remaining_segments = remaining_segments[pr.first_failed_position:]
                    logger.warning(
                        "分块部分翻译成功 %d/%d，重试剩余 %d 条 (第 %d 次)",
                        len(pr.matched), len(remaining_ids),
                        len(remaining_segments), attempt + 1,
                    )
                    continue
                else:
                    remaining_segments = []
                    break

            # Nothing matched at all on this attempt
            if attempt < MAX_PARTIAL_RETRIES:
                logger.warning(
                    "分块翻译完全失败，重试整块 (第 %d 次)", attempt + 1,
                )
                continue
            else:
                raise PipelineError(
                    f"分块翻译经过 {MAX_PARTIAL_RETRIES + 1} 次尝试仍无法解析"
                )

        # Fill any remaining None with source text as last resort
        final: list[str] = []
        for i, val in enumerate(results):
            if val is None:
                logger.warning("翻译缺失行 %d，回退为原文", chunk.main_segments[i].index)
                final.append(texts[i])
            else:
                final.append(val)
        return final

    def _resolve_base_url(self) -> str:
        return self.client.resolved_base_url()

    def _request_translation(self, prompt: str, user_content: str) -> str:
        try:
            return self.client.complete(
                [
                    LLMMessage(role="system", content=prompt),
                    LLMMessage(role="user", content=user_content),
                ]
            )
        except LLMRateLimitError as exc:
            raise TranslationRateLimitError(str(exc)) from exc
        except LLMError as exc:
            raise PipelineError(str(exc)) from exc

    def _build_prompt(self, target_language: str, content_type: str = "general", custom_prompt: str = "") -> str:
        style_prompt = custom_prompt.strip() or TRANSLATION_PRESETS.get(content_type, TRANSLATION_PRESETS["general"])
        return f"{style_prompt}\n\n{FORMAT_INSTRUCTION.format(target_language=target_language)}"

    def _parse_json_array_content(self, content: str) -> list[Any]:
        candidates = [
            content.strip(),
            strip_code_fence(content),
            self._extract_json_array(content),
        ]
        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                parsed = json.loads(normalized)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                return parsed
        raise PipelineError(f"真实翻译 provider 未返回 JSON 数组，原始响应: {content[:200]}")

    def _strip_code_fence(self, content: str) -> str:
        return strip_code_fence(content)

    def _extract_json_array(self, content: str) -> str:
        start = content.find("[")
        end = content.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return ""
        return content[start : end + 1]


# ---------------------------------------------------------------------------
# Debug helper (used by backend/debug_translation.py CLI + test_mvp.py)
# ---------------------------------------------------------------------------


def debug_translation_request(
    llm_type: str,
    api_base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: int,
    target_language: str,
    texts: list[str],
    content_type: str = "general",
    custom_prompt: str = "",
) -> dict[str, Any]:
    provider = LLMTranslationProvider(
        llm_type=llm_type,
        api_base_url=api_base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )
    chunk = TranslationChunk(
        start_index=0,
        context_before=[],
        main_segments=[ChunkLine(index=index, text=text) for index, text in enumerate(texts)],
        context_after=[],
        use_sections=False,
    )
    prompt = provider._build_prompt(target_language, content_type, custom_prompt)
    content = provider._request_translation(prompt, build_chunk_user_message(chunk))
    parsed = parse_chunk_output(content, [line.index for line in chunk.main_segments], texts, provider._parse_json_array_content)
    return {
        "llm_type": llm_type,
        "base_url": provider._resolve_base_url(),
        "model": model,
        "target_language": target_language,
        "texts": texts,
        "raw_content": content,
        "parsed": [str(item).strip() for item in parsed],
    }


__all__ = [
    # constants
    "CHUNK_SIZE",
    "CONTEXT_SIZE",
    "MAX_CHUNK_WORKERS",
    "MAX_CHUNK_RETRIES",
    "MAX_PARTIAL_RETRIES",
    "TRANSLATION_PRESETS",
    "FORMAT_INSTRUCTION",
    # exceptions
    "TranslationRateLimitError",
    # types
    "ChunkLine",
    "TranslationChunk",
    "TranslationProvider",
    "ParseResult",
    # helpers
    "build_chunks",
    "build_chunk_user_message",
    "strip_code_fence",
    "strip_number_prefix",
    "parse_numbered_lines_ordered",
    "parse_numbered_lines",
    "strip_think",
    "parse_chunk_output",
    "_translation_cache_key",
    # classes
    "ChunkedTranslator",
    "LLMTranslationProvider",
    # debug
    "debug_translation_request",
]
