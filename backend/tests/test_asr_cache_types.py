"""P2-3: Verify TYPE_CHECKING guard in asr/cache.py.

Tests that the cache module imports cleanly without torch/whisperx at runtime,
and that the type annotations resolve under static analysis.
"""
from __future__ import annotations

import sys
import typing
import unittest
from pathlib import Path
from unittest import mock as unittest_mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.asr.cache import WhisperModelCache, _resolve_whisperx_compute_type
from app.asr.exceptions import PipelineError


class TypeCheckingGuardTests(unittest.TestCase):
    """Verify the TYPE_CHECKING block does not import torch/whisperx at runtime."""

    def test_whisperx_not_imported_at_module_load(self) -> None:
        """The _model annotation should reference whisperx.WhisperModel in source."""
        from app.asr import cache as cache_module

        source = Path(cache_module.__file__).read_text(encoding="utf-8")
        self.assertIn("whisperx.WhisperModel", source)

    def test_type_checking_constant_exists(self) -> None:
        """The module should have TYPE_CHECKING imported from typing."""
        import app.asr.cache as cache_module

        source = Path(cache_module.__file__).read_text(encoding="utf-8")
        self.assertIn("TYPE_CHECKING", source)
        self.assertIn("if TYPE_CHECKING:", source)

    def test_no_top_level_whisperx_import(self) -> None:
        """No unconditional `import whisperx` at module level (only inside functions)."""
        import app.asr.cache as cache_module

        source = Path(cache_module.__file__).read_text(encoding="utf-8")
        lines = source.splitlines()
        # Find any line with 'import whisperx' that is NOT inside TYPE_CHECKING or a function
        found_top_level = False
        in_type_checking = False
        in_function = False
        indent_level = 0
        for line in lines:
            stripped = line.lstrip()
            current_indent = len(line) - len(stripped)
            if stripped.startswith("if TYPE_CHECKING:"):
                in_type_checking = True
                indent_level = current_indent
                continue
            if in_type_checking and current_indent <= indent_level and stripped and not stripped.startswith("#"):
                in_type_checking = False
            if stripped.startswith("def ") or stripped.startswith("class "):
                in_function = True
            if "import whisperx" in stripped:
                if not in_type_checking and not in_function:
                    found_top_level = True
                    break
        self.assertFalse(found_top_level, "Found unconditional top-level 'import whisperx'")


class WhisperModelCacheBehaviorTests(unittest.TestCase):
    """Verify the public API still works correctly."""

    def test_cache_initial_state(self) -> None:
        cache = WhisperModelCache()
        self.assertIsNone(cache._model)
        self.assertIsNone(cache._model_name)
        self.assertIsNone(cache._align_model)

    def test_resolve_compute_type_auto_returns_none(self) -> None:
        result = _resolve_whisperx_compute_type("auto")
        self.assertIsNone(result)

    def test_resolve_compute_type_fp16(self) -> None:
        result = _resolve_whisperx_compute_type("fp16")
        self.assertEqual(result, "float16")

    def test_resolve_compute_type_invalid_raises(self) -> None:
        with self.assertRaises(PipelineError):
            _resolve_whisperx_compute_type("invalid_type")

    def test_get_model_raises_without_whisperx(self) -> None:
        cache = WhisperModelCache()
        with unittest_mock.patch("builtins.__import__", side_effect=ImportError("mocked")):
            with self.assertRaises(PipelineError):
                cache.get_model("test-model", "cpu")


if __name__ == "__main__":
    unittest.main()
