"""Tests for the pipeline refactor that extracted translation + SRT helpers.

Verifies:
  1. The extracted ``app.llm.translation`` module is importable standalone.
  2. The extracted ``app.subtitle.srt`` module is importable standalone.
  3. ``format_srt_time`` edge cases (0, negative, >24h, fractional).
  4. ``build_srt_content`` bilingual merge output.
  5. ``build_srt_content`` bilingual separate (replace_source) output.
  6. ``build_srt_content`` produces structurally valid SRT.
  7. ``write_stage_artifacts`` + ``read_stage_artifacts`` round-trip.
  8-10. Backward-compat re-exports from ``app.pipeline``.
  11. ``debug_translation_request`` is still importable from ``app.pipeline``.
  12. ``TaskContext`` and ``PipelineError`` still importable from ``app.pipeline``.

Run via:
    python -m unittest backend.tests.test_pipeline_refactor
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ChunkedTranslatorModuleImportTests(unittest.TestCase):
    """1. ``ChunkedTranslator`` extracted module is importable and works."""

    def test_import_from_llm_translation(self) -> None:
        from app.llm.translation import ChunkedTranslator

        self.assertTrue(callable(ChunkedTranslator))

    def test_class_instantiation_with_mock_provider(self) -> None:
        from app.llm.translation import ChunkedTranslator, TranslationProvider

        provider = TranslationProvider()
        translator = ChunkedTranslator(provider, "general", "")
        self.assertIs(translator.provider, provider)
        self.assertEqual(translator.content_type, "general")
        # Empty segment list short-circuits to empty translations.
        self.assertEqual(translator.translate_language([], "zh"), [])


class LLMTranslationProviderModuleImportTests(unittest.TestCase):
    """2. ``LLMTranslationProvider`` extracted module is importable and works."""

    @patch("app.llm.providers.OpenAI")
    def test_import_and_construct(self, mock_openai: MagicMock) -> None:
        mock_openai.return_value = MagicMock()
        from app.llm.translation import LLMTranslationProvider

        provider = LLMTranslationProvider(
            llm_type="openai-compatible",
            api_base_url="https://api.openai.com",
            api_key="test-key",
            model="gpt-4o-mini",
            timeout_seconds=30,
        )
        self.assertEqual(provider.model, "gpt-4o-mini")
        self.assertEqual(provider.api_key, "test-key")

    @patch("app.llm.providers.OpenAI")
    def test_build_prompt_uses_presets(self, mock_openai: MagicMock) -> None:
        mock_openai.return_value = MagicMock()
        from app.llm.translation import LLMTranslationProvider, TRANSLATION_PRESETS

        provider = LLMTranslationProvider(
            llm_type="openai-compatible",
            api_base_url="https://api.openai.com",
            api_key="k",
            model="m",
            timeout_seconds=30,
        )
        prompt = provider._build_prompt("zh-CN", "movie", "")
        self.assertIn(TRANSLATION_PRESETS["movie"], prompt)


class FormatSrtTimeEdgeCaseTests(unittest.TestCase):
    """3. ``format_srt_time`` handles edge cases."""

    def test_zero(self) -> None:
        from app.subtitle.srt import format_srt_time

        self.assertEqual(format_srt_time(0), "00:00:00,000")

    def test_negative_clamped_to_zero(self) -> None:
        from app.subtitle.srt import format_srt_time

        self.assertEqual(format_srt_time(-5.0), "00:00:00,000")

    def test_fractional_millisecond_rounding(self) -> None:
        from app.subtitle.srt import format_srt_time

        # 1.2345 * 1000 = 1234.499... in float64 → rounds to 1234
        self.assertEqual(format_srt_time(1.2345), "00:00:01,234")
        # 2.9999 → 2999.9… → rounds to 3000 → carries to next second
        self.assertEqual(format_srt_time(2.9999), "00:00:03,000")

    def test_greater_than_24h(self) -> None:
        from app.subtitle.srt import format_srt_time

        # 25h 1m 2s → 25:01:02,000  (SRT hours are unbounded)
        result = format_srt_time(25 * 3600 + 62)
        self.assertEqual(result, "25:01:02,000")

    def test_standard_values(self) -> None:
        from app.subtitle.srt import format_srt_time

        self.assertEqual(format_srt_time(1.5), "00:00:01,500")
        self.assertEqual(format_srt_time(61.0), "00:01:01,000")
        self.assertEqual(format_srt_time(3661.0), "01:01:01,000")


class BuildSrtContentBilingualMergeTests(unittest.TestCase):
    """4. ``build_srt_content`` bilingual merge output."""

    def test_bilingual_merge_produces_two_lines_per_block(self) -> None:
        from app.subtitle.srt import build_srt_content

        segments = [
            {"start": 0.0, "end": 1.0, "text": "Hello"},
            {"start": 1.0, "end": 2.0, "text": "World"},
        ]
        translations = ["你好", "世界"]
        content = build_srt_content(segments, translations)
        blocks = content.strip().split("\n\n")
        self.assertEqual(len(blocks), 2)
        first_block_lines = blocks[0].split("\n")
        # index, timestamp, source, translation
        self.assertEqual(first_block_lines[0], "1")
        self.assertIn("-->", first_block_lines[1])
        self.assertEqual(first_block_lines[2], "Hello")
        self.assertEqual(first_block_lines[3], "你好")


class BuildSrtContentBilingualSeparateTests(unittest.TestCase):
    """5. ``build_srt_content`` bilingual separate (replace_source) output."""

    def test_replace_source_shows_only_translation(self) -> None:
        from app.subtitle.srt import build_srt_content

        segments = [{"start": 0.0, "end": 1.0, "text": "Hello"}]
        translations = ["你好"]
        content = build_srt_content(segments, translations, replace_source=True)
        block_lines = content.strip().split("\n")
        # index, timestamp, translation-only
        self.assertEqual(block_lines[0], "1")
        self.assertEqual(block_lines[2], "你好")
        self.assertNotIn("Hello", content)


class BuildSrtContentValidFormatTests(unittest.TestCase):
    """6. ``build_srt_content`` produces valid SRT format."""

    def test_ends_with_newline(self) -> None:
        from app.subtitle.srt import build_srt_content

        content = build_srt_content([{"start": 0, "end": 1, "text": "x"}], None)
        self.assertTrue(content.endswith("\n"))

    def test_no_translations_produces_source_only(self) -> None:
        from app.subtitle.srt import build_srt_content

        segments = [{"start": 0.0, "end": 2.0, "text": "Original"}]
        content = build_srt_content(segments, None)
        self.assertIn("Original", content)
        self.assertIn("00:00:00,000 --> 00:00:02,000", content)

    def test_timestamps_use_srt_format(self) -> None:
        from app.subtitle.srt import build_srt_content

        segments = [{"start": 5.0, "end": 10.5, "text": "x"}]
        content = build_srt_content(segments, None)
        self.assertIn("00:00:05,000 --> 00:00:10,500", content)


class StageArtifactsRoundTripTests(unittest.TestCase):
    """7. ``write_stage_artifacts`` + ``read_stage_artifacts`` preserves data."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.tmp.name) / "work"
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_context(self) -> Any:
        from app.pipeline import TaskContext

        return TaskContext(
            task_id=1,
            file_path="/fake/video.mp4",
            config_snapshot={},  # type: ignore[arg-type]
            work_dir=self.work_dir,
        )

    def test_roundtrip_preserves_payload(self) -> None:
        from app.subtitle.srt import read_stage_artifacts, write_stage_artifacts

        payload = {"audio_path": "/tmp/a.wav", "device": "cpu", "translations": ["zh"]}
        ctx = self._make_context()
        write_stage_artifacts(ctx, payload)
        result = read_stage_artifacts(ctx)
        self.assertEqual(result, payload)

    def test_read_missing_raises_pipeline_error(self) -> None:
        from app.pipeline import PipelineError
        from app.subtitle.srt import read_stage_artifacts

        ctx = self._make_context()
        with self.assertRaises(PipelineError):
            read_stage_artifacts(ctx)

    def test_read_invalid_json_raises_pipeline_error(self) -> None:
        from app.pipeline import PipelineError
        from app.subtitle.srt import read_stage_artifacts

        ctx = self._make_context()
        (self.work_dir / "artifacts.json").write_text("[1,2,3]", encoding="utf-8")
        with self.assertRaises(PipelineError):
            read_stage_artifacts(ctx)


class BackwardCompatReExportTests(unittest.TestCase):
    """8-10. All moved symbols are re-exported from ``app.pipeline``."""

    def test_chunked_translator_from_pipeline(self) -> None:
        from app.pipeline import ChunkedTranslator  # noqa: F401

    def test_llm_translation_provider_from_pipeline(self) -> None:
        from app.pipeline import LLMTranslationProvider  # noqa: F401

    def test_format_srt_time_from_pipeline(self) -> None:
        from app.pipeline import format_srt_time

        self.assertEqual(format_srt_time(0), "00:00:00,000")

    def test_build_srt_content_from_pipeline(self) -> None:
        from app.pipeline import build_srt_content

        content = build_srt_content([{"start": 0, "end": 1, "text": "hi"}], None)
        self.assertIn("hi", content)

    def test_translation_constants_from_pipeline(self) -> None:
        from app.pipeline import (
            CHUNK_SIZE,
            CONTEXT_SIZE,
            FORMAT_INSTRUCTION,
            MAX_CHUNK_RETRIES,
            MAX_CHUNK_WORKERS,
            MAX_PARTIAL_RETRIES,
            TRANSLATION_PRESETS,
        )

        self.assertEqual(CHUNK_SIZE, 15)
        self.assertEqual(CONTEXT_SIZE, 5)
        self.assertIn("general", TRANSLATION_PRESETS)
        self.assertIn("movie", TRANSLATION_PRESETS)
        self.assertIn("{target_language}", FORMAT_INSTRUCTION)
        self.assertGreaterEqual(MAX_CHUNK_RETRIES, 1)
        self.assertGreaterEqual(MAX_CHUNK_WORKERS, 1)
        self.assertGreaterEqual(MAX_PARTIAL_RETRIES, 1)

    def test_translation_helpers_from_pipeline(self) -> None:
        from app.pipeline import (
            ChunkLine,
            ParseResult,
            TranslationChunk,
            TranslationProvider,
            TranslationRateLimitError,
            _translation_cache_key,
            build_chunk_user_message,
            build_chunks,
            parse_chunk_output,
            parse_numbered_lines,
            parse_numbered_lines_ordered,
            strip_code_fence,
            strip_number_prefix,
            strip_think,
        )

        # Smoke-test a couple to ensure they are the real callables
        self.assertEqual(strip_number_prefix("1|你好"), "你好")
        chunk = TranslationChunk(0, [], [ChunkLine(0, "a")], [], False)
        self.assertEqual(_translation_cache_key(chunk, "zh", "openai-chat", "m", "general", "")[1], "zh")


class DebugTranslationRequestImportTests(unittest.TestCase):
    """11. ``debug_translation_request`` is still importable from app.pipeline."""

    def test_importable_from_pipeline(self) -> None:
        from app.pipeline import debug_translation_request

        self.assertTrue(callable(debug_translation_request))

    def test_importable_from_llm_translation(self) -> None:
        from app.llm.translation import debug_translation_request

        self.assertTrue(callable(debug_translation_request))

    def test_same_object_via_both_paths(self) -> None:
        from app.llm.translation import debug_translation_request as direct
        from app.pipeline import debug_translation_request as reexported

        self.assertIs(direct, reexported)


class TaskContextAndPipelineErrorTests(unittest.TestCase):
    """12. ``TaskContext`` and ``PipelineError`` still importable from app.pipeline."""

    def test_task_context_importable(self) -> None:
        from app.pipeline import TaskContext

        ctx = TaskContext(
            task_id=1,
            file_path="/x.mp4",
            config_snapshot={},
            work_dir=Path("/tmp"),
        )
        self.assertEqual(ctx.task_id, 1)

    def test_pipeline_error_importable(self) -> None:
        from app.pipeline import PipelineError

        with self.assertRaises(PipelineError):
            raise PipelineError("test")

    def test_translation_rate_limit_error_subclasses_pipeline_error(self) -> None:
        from app.pipeline import PipelineError, TranslationRateLimitError

        self.assertTrue(issubclass(TranslationRateLimitError, PipelineError))


if __name__ == "__main__":
    unittest.main()
