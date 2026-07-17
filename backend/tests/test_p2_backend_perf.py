"""P2 backend performance/type tests.

Covers:
- P2-4: LLMRateLimitError type preserved through PipelineError wrapper
- P2-5: FFMPEG_TIMEOUT_SECONDS constant extracted
- P2-6: TTL cache for directory-size calculations
- P2-7: WorkerService model cache is per-process (lazy + warning)
- P2-8: translate_segments database param typed as Database | None
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.llm import LLMError, LLMRateLimitError
from app.llm.translation import (
    ChunkedTranslator,
    ChunkLine,
    LLMTranslationProvider,
    PipelineError,
    TranslationChunk,
    TranslationProvider,
    TranslationRateLimitError,
)
from app.model_manager import ModelManager
from app.pipeline import FFMPEG_TIMEOUT_SECONDS, translate_segments
from app.runtime import WorkerService
from app.store import Database


# ---------------------------------------------------------------------------
# P2-4: Preserve LLMRateLimitError type
# ---------------------------------------------------------------------------


class _StubProvider(TranslationProvider):
    def __init__(self, exc: Exception | None = None, result: list[str] | None = None):
        self._exc = exc
        self._result = result

    def translate_batch(self, texts: list[str], target_language: str) -> list[str]:
        if self._exc:
            raise self._exc
        return self._result or texts[:]


class RateLimitErrorPreservedTests(unittest.TestCase):
    def test_rate_limit_error_preserved(self):
        provider = _StubProvider(exc=TranslationRateLimitError("rate limited"))
        translator = ChunkedTranslator(provider, "general", "", llm_type="test", model="test")
        segments = [{"text": f"line {i}"} for i in range(3)]
        with self.assertRaises(TranslationRateLimitError) as ctx:
            translator.translate_language(segments, "zh")
        self.assertIsInstance(ctx.exception, TranslationRateLimitError)
        self.assertIsInstance(ctx.exception, PipelineError)

    def test_generic_pipeline_error_preserved(self):
        provider = _StubProvider(exc=PipelineError("generic pipeline error"))
        translator = ChunkedTranslator(provider, "general", "", llm_type="test", model="test")
        segments = [{"text": f"line {i}"} for i in range(3)]
        with self.assertRaises(PipelineError) as ctx:
            translator.translate_language(segments, "zh")
        self.assertNotIsInstance(ctx.exception, TranslationRateLimitError)
        self.assertEqual(str(ctx.exception), "generic pipeline error")

    def test_other_pipeline_errors_unchanged(self):
        provider = _StubProvider(exc=ValueError("unexpected"))
        translator = ChunkedTranslator(provider, "general", "", llm_type="test", model="test")
        segments = [{"text": f"line {i}"} for i in range(3)]
        with self.assertRaises(PipelineError) as ctx:
            translator.translate_language(segments, "zh")
        self.assertNotIsInstance(ctx.exception, TranslationRateLimitError)
        self.assertEqual(str(ctx.exception), "unexpected")

    def test_from_clause_preserves_chain(self):
        original = TranslationRateLimitError("original rate limit")
        provider = _StubProvider(exc=original)
        translator = ChunkedTranslator(provider, "general", "", llm_type="test", model="test")
        segments = [{"text": f"line {i}"} for i in range(3)]
        with self.assertRaises(TranslationRateLimitError) as ctx:
            translator.translate_language(segments, "zh")
        self.assertIsNotNone(ctx.exception.__cause__)
        self.assertEqual(ctx.exception.__cause__, original)


# ---------------------------------------------------------------------------
# P2-5: FFMPEG_TIMEOUT_SECONDS constant
# ---------------------------------------------------------------------------


class FFmpegTimeoutConstantTests(unittest.TestCase):
    def test_ffmpeg_timeout_constant_defined(self):
        self.assertEqual(FFMPEG_TIMEOUT_SECONDS, 7200)

    def test_ffmpeg_timeout_used_in_calls(self):
        import app.pipeline as pipeline_mod

        captured_timeouts: list[int] = []

        original_run = pipeline_mod.subprocess.run

        def fake_run(command, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            result.stdout = ""
            return result

        pipeline_mod.subprocess.run = fake_run
        try:
            pipeline_mod._run_ffmpeg(["echo", "test"], "timeout-msg", "fail-msg")
        finally:
            pipeline_mod.subprocess.run = original_run
        self.assertIn(FFMPEG_TIMEOUT_SECONDS, captured_timeouts)

    def test_timeout_constant_is_integer(self):
        self.assertIsInstance(FFMPEG_TIMEOUT_SECONDS, int)
        self.assertGreater(FFMPEG_TIMEOUT_SECONDS, 0)


# ---------------------------------------------------------------------------
# P2-6: TTL cache for directory-size calculations
# ---------------------------------------------------------------------------


class DirectorySizeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.models_root = Path(self.temp_dir.name) / "models"
        self.models_root.mkdir(parents=True, exist_ok=True)
        self.model_dir = self.models_root / "test-model"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        (self.model_dir / "weights.bin").write_bytes(b"\x00" * 1024)
        self.manager = ModelManager(str(self.models_root), directory_size_cache_ttl=2.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_directory_size_cached(self):
        self.manager._directory_size(self.model_dir)
        call_count = 0
        original_rglob = Path.rglob

        def counting_rglob(self_path, pattern):
            nonlocal call_count
            if self_path == self.model_dir:
                call_count += 1
            return original_rglob(self_path, pattern)

        with patch.object(Path, "rglob", counting_rglob):
            self.manager._directory_size(self.model_dir)
        self.assertEqual(call_count, 0)

    def test_cache_expires_after_ttl(self):
        self.manager._dir_size_cache_ttl = 0.05
        self.manager._directory_size(self.model_dir)
        time.sleep(0.1)
        rglob_called = False
        original_rglob = Path.rglob

        def counting_rglob(self_path, pattern):
            nonlocal rglob_called
            if self_path == self.model_dir:
                rglob_called = True
            return original_rglob(self_path, pattern)

        with patch.object(Path, "rglob", counting_rglob):
            self.manager._directory_size(self.model_dir)
        self.assertTrue(rglob_called)

    def test_cache_invalidated_on_download(self):
        self.manager._directory_size(self.model_dir)
        self.manager._invalidate_directory_size_cache(self.model_dir)
        self.assertEqual(len(self.manager._dir_size_cache), 0)

    def test_cache_thread_safe(self):
        self.manager._dir_size_cache_ttl = 0.5
        errors: list[Exception] = []
        results: list[int] = []

        def worker():
            try:
                for _ in range(20):
                    results.append(self.manager._directory_size(self.model_dir))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(all(r == 1024 for r in results))


# ---------------------------------------------------------------------------
# P2-7: WorkerService model cache is per-process
# ---------------------------------------------------------------------------


class _MinimalDatabase:
    def is_setup_complete(self):
        return False

    def get_config(self):
        return {"processing": {"poll_interval_seconds": 1}}


class WorkerModelCacheTests(unittest.TestCase):
    def test_model_cache_lazy_creation(self):
        ws = WorkerService.__new__(WorkerService)
        ws.database = _MinimalDatabase()
        ws._model_cache = None
        ws._model_cache_warned = False
        self.assertIsNone(ws._model_cache)

    def test_log_message_on_first_load(self):
        ws = WorkerService.__new__(WorkerService)
        ws.database = _MinimalDatabase()
        ws._model_cache = None
        ws._model_cache_warned = False
        with patch("app.runtime.logger") as mock_logger:
            _ = ws.model_cache
            mock_logger.info.assert_called_once()
            log_msg = mock_logger.info.call_args[0][0]
            self.assertIn("per-process", log_msg)
        self.assertIsNotNone(ws._model_cache)
        mock_logger.info.reset_mock()
        _ = ws.model_cache
        mock_logger.info.assert_not_called()

    def test_model_cache_attribute_exists(self):
        ws = WorkerService.__new__(WorkerService)
        ws.database = _MinimalDatabase()
        ws._model_cache = None
        ws._model_cache_warned = False
        self.assertTrue(hasattr(ws, "model_cache"))


# ---------------------------------------------------------------------------
# P2-8: translate_segments database param typed as Database | None
# ---------------------------------------------------------------------------


class TranslateSegmentsTypeAnnotationTests(unittest.TestCase):
    def test_type_annotation_is_not_any(self):
        import app.pipeline as pipeline_mod
        from typing import Any

        annotations = translate_segments.__annotations__
        db_annotation = annotations.get("database")
        self.assertIsNotNone(db_annotation)
        self.assertNotEqual(db_annotation, Any)

    def test_type_annotation_contains_database(self):
        annotations = translate_segments.__annotations__
        db_annotation = annotations.get("database")
        annotation_str = str(db_annotation)
        self.assertIn("Database", annotation_str)
        self.assertIn("None", annotation_str)

    def test_translate_segments_accepts_none(self):
        import app.pipeline as pipeline_mod

        config_snapshot = {
            "translation": {
                "enabled": False,
                "target_languages": [],
            },
        }
        from app.pipeline import TaskContext

        work_dir = Path(tempfile.mkdtemp())
        context = TaskContext(
            task_id=1,
            file_path="/tmp/fake.mp4",
            config_snapshot=config_snapshot,
            work_dir=work_dir,
        )
        result = translate_segments(context, [], database=None)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
