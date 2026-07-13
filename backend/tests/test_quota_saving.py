from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline import (  # noqa: E402
    ChunkLine,
    TranslationChunk,
    _translation_cache_key,
    expected_target_subtitle_paths,
)
from app.store import Database  # noqa: E402


def _chunk(texts: list[str], start_index: int = 0) -> TranslationChunk:
    main = [ChunkLine(index=start_index + i, text=t) for i, t in enumerate(texts)]
    return TranslationChunk(
        start_index=start_index,
        context_before=[],
        main_segments=main,
        context_after=[],
        use_sections=False,
    )


class ExpectedTargetSubtitlePathsTests(unittest.TestCase):
    """功能 A:已有目标字幕探测的命名规则。"""

    def test_bilingual_merge_returns_single_bilingual_path(self) -> None:
        paths = expected_target_subtitle_paths(
            Path("/data/movie.mkv"),
            {"output_to_source_dir": True},
            {"bilingual": True, "bilingual_mode": "merge", "filename_template": "{stem}.forced.{lang}.srt"},
            {"enabled": True, "target_languages": ["zh"]},
        )
        self.assertEqual(paths, [Path("/data/movie.forced.bilingual.srt")])

    def test_separate_mode_returns_one_path_per_language(self) -> None:
        paths = expected_target_subtitle_paths(
            Path("/data/movie.mkv"),
            {"output_to_source_dir": True},
            {"bilingual": True, "bilingual_mode": "separate", "filename_template": "{stem}.forced.{lang}.srt"},
            {"enabled": True, "target_languages": ["zh", "en"]},
        )
        self.assertEqual(paths, [Path("/data/movie.forced.zh.srt"), Path("/data/movie.forced.en.srt")])

    def test_translation_disabled_returns_empty(self) -> None:
        paths = expected_target_subtitle_paths(
            Path("/data/movie.mkv"),
            {"output_to_source_dir": True},
            {"bilingual": True, "bilingual_mode": "merge", "filename_template": "{stem}.forced.{lang}.srt"},
            {"enabled": False, "target_languages": ["zh"]},
        )
        self.assertEqual(paths, [])


class TranslationCacheKeyTests(unittest.TestCase):
    """功能 B:缓存键稳定性 —— 同文本同键、换上下文即失效。"""

    def test_same_text_same_key(self) -> None:
        k1 = _translation_cache_key(_chunk(["你好", "世界"]), "zh", "openai-chat", "gpt-4o-mini", "general", "")
        k2 = _translation_cache_key(_chunk(["你好", "世界"]), "zh", "openai-chat", "gpt-4o-mini", "general", "")
        self.assertEqual(k1, k2)

    def test_different_model_different_key(self) -> None:
        k1 = _translation_cache_key(_chunk(["你好"]), "zh", "openai-chat", "gpt-4o-mini", "general", "")
        k2 = _translation_cache_key(_chunk(["你好"]), "zh", "openai-chat", "gpt-4o", "general", "")
        self.assertNotEqual(k1, k2)

    def test_different_custom_prompt_different_key(self) -> None:
        k1 = _translation_cache_key(_chunk(["你好"]), "zh", "openai-chat", "gpt-4o-mini", "general", "")
        k2 = _translation_cache_key(_chunk(["你好"]), "zh", "openai-chat", "gpt-4o-mini", "general", "语气轻松")
        self.assertNotEqual(k1, k2)

    def test_different_target_language_different_key(self) -> None:
        k1 = _translation_cache_key(_chunk(["你好"]), "zh", "openai-chat", "gpt-4o-mini", "general", "")
        k2 = _translation_cache_key(_chunk(["你好"]), "ja", "openai-chat", "gpt-4o-mini", "general", "")
        self.assertNotEqual(k1, k2)


class TranslationCacheStoreTests(unittest.TestCase):
    """功能 B:translation_cache 表的 get/set/UPSERT 端到端。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_miss_returns_none(self) -> None:
        key = ("hash-1", "zh", "openai-chat", "gpt-4o-mini", "general", "cp1")
        self.assertIsNone(self.db.get_translation_cache(*key))

    def test_set_then_get_roundtrip(self) -> None:
        key = ("hash-2", "zh", "openai-chat", "gpt-4o-mini", "general", "cp1")
        self.db.set_translation_cache(*key, ["你好", "世界"])
        self.assertEqual(self.db.get_translation_cache(*key), ["你好", "世界"])

    def test_upsert_overwrites_existing(self) -> None:
        key = ("hash-3", "zh", "openai-chat", "gpt-4o-mini", "general", "cp1")
        self.db.set_translation_cache(*key, ["旧译文"])
        self.db.set_translation_cache(*key, ["新译文"])
        self.assertEqual(self.db.get_translation_cache(*key), ["新译文"])


if __name__ == "__main__":
    unittest.main()
