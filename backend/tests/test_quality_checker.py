"""Tests for the post-pipeline quality checker.

Covers:
- Empty / None input is safe
- Clean data → score 100, no suspect
- Each individual check rule (low confidence, short/long segment, repetition,
  empty translation, char ratio, tiny segment count) fires when its
  condition is met and stays quiet otherwise
- Score penalty formula: errors -25, warnings -8, clamped to [0, 100]
- is_suspect flips true on score < threshold OR any error severity
- to_dict round-trip is JSON-safe (no dataclass leaks)
- Custom config thresholds override defaults
- The `translations` schema-mismatch (length differs) is silently skipped,
  not flagged as a quality issue
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.quality_checker import (
    ISSUE_EMPTY_TRANSLATION,
    ISSUE_LOW_CONFIDENCE,
    ISSUE_LONG_SEGMENT,
    ISSUE_REPEATED_SEGMENT,
    ISSUE_SHORT_SEGMENT,
    ISSUE_TINY_SEGMENT_COUNT,
    ISSUE_TRANSLATION_RATIO,
    check_quality,
    is_suspect_report,
)


def _clean_segs():
    return [
        {"start": 0.0, "end": 2.0, "text": "Hello world this is a test"},
        {"start": 2.0, "end": 4.5, "text": "How are you doing today"},
        {"start": 4.5, "end": 7.0, "text": "I am doing great thanks"},
        {"start": 7.0, "end": 9.0, "text": "What is the weather like"},
        {"start": 9.0, "end": 11.0, "text": "It is sunny outside"},
    ]


def _clean_zh():
    return [
        "你好世界这是一个测试",
        "你今天怎么样",
        "我做得很好谢谢",
        "今天天气怎么样",
        "外面阳光明媚",
    ]


class InputSafetyTests(unittest.TestCase):
    def test_none_returns_score_none(self) -> None:
        r = check_quality(None)
        self.assertIsNone(r.score)
        self.assertFalse(r.is_suspect)
        self.assertEqual(r.issues, [])
        self.assertEqual(r.suspect_segment_ids, [])

    def test_empty_list_returns_score_none(self) -> None:
        r = check_quality([])
        self.assertIsNone(r.score)
        self.assertFalse(r.is_suspect)


class CleanDataTests(unittest.TestCase):
    def test_clean_run_scores_100(self) -> None:
        r = check_quality(_clean_segs(), {"zh": _clean_zh()})
        self.assertEqual(r.score, 100)
        self.assertFalse(r.is_suspect)
        self.assertEqual(r.issues, [])
        self.assertIn("质量良好", r.summary)

    def test_clean_run_with_no_translations_still_ok(self) -> None:
        r = check_quality(_clean_segs())
        self.assertEqual(r.score, 100)
        self.assertFalse(r.is_suspect)


class LowConfidenceTests(unittest.TestCase):
    def test_low_avg_confidence_flagged(self) -> None:
        # Each segment's ASR confidence averages below 0.6 default.
        asr_segs = [
            {"start": 0.0, "end": 2.0, "text": "Hello", "score": 0.3},
            {"start": 2.0, "end": 4.5, "text": "World", "score": 0.4},
        ]
        r = check_quality(_clean_segs(), asr_segments=asr_segs)
        self.assertTrue(r.is_suspect)
        self.assertTrue(any(i.code == ISSUE_LOW_CONFIDENCE for i in r.issues))

    def test_word_level_confidence_aggregates(self) -> None:
        # word-level scores inside each segment get averaged
        asr_segs = [
            {"start": 0.0, "end": 2.0, "text": "Hi", "words": [{"score": 0.3}, {"score": 0.4}]},
            {"start": 2.0, "end": 4.0, "text": "World", "words": [{"score": 0.5}, {"score": 0.5}]},
        ]
        r = check_quality(_clean_segs(), asr_segments=asr_segs)
        # mean of [0.3, 0.4, 0.5, 0.5] = 0.425 → below 0.6
        self.assertTrue(any(i.code == ISSUE_LOW_CONFIDENCE for i in r.issues))

    def test_high_confidence_not_flagged(self) -> None:
        asr_segs = [
            {"start": 0.0, "end": 2.0, "text": "Hi", "score": 0.95},
            {"start": 2.0, "end": 4.0, "text": "World", "score": 0.9},
        ]
        r = check_quality(_clean_segs(), asr_segments=asr_segs)
        self.assertFalse(any(i.code == ISSUE_LOW_CONFIDENCE for i in r.issues))

    def test_no_confidence_data_skips_check(self) -> None:
        # No score fields anywhere — check should be a no-op
        r = check_quality(_clean_segs())
        self.assertFalse(any(i.code == ISSUE_LOW_CONFIDENCE for i in r.issues))


class DurationAnomalyTests(unittest.TestCase):
    def test_short_segment_flagged(self) -> None:
        segs = _clean_segs() + [{"start": 11.0, "end": 11.2, "text": "ok"}]
        r = check_quality(segs, {"zh": _clean_zh() + ["好"]})
        codes = [i.code for i in r.issues]
        self.assertIn(ISSUE_SHORT_SEGMENT, codes)

    def test_long_segment_flagged(self) -> None:
        segs = _clean_segs() + [{"start": 11.0, "end": 30.0, "text": "very long monolog"}]
        r = check_quality(segs, {"zh": _clean_zh() + ["非常长的独白"]})
        codes = [i.code for i in r.issues]
        self.assertIn(ISSUE_LONG_SEGMENT, codes)

    def test_zero_duration_segments_ignored(self) -> None:
        segs = [{"start": 0.0, "end": 0.0, "text": ""} for _ in range(5)]
        r = check_quality(segs)
        # zero-duration segments are not "too short" (they're empty / artifacts)
        self.assertFalse(any(i.code == ISSUE_SHORT_SEGMENT for i in r.issues))


class RepetitionTests(unittest.TestCase):
    def test_consecutive_repetition_flagged(self) -> None:
        segs = [{"start": i * 2.0, "end": (i + 1) * 2.0, "text": "same text"} for i in range(5)]
        r = check_quality(segs)
        self.assertTrue(r.is_suspect)
        self.assertTrue(any(i.code == ISSUE_REPEATED_SEGMENT for i in r.issues))

    def test_max_repeat_threshold_configurable(self) -> None:
        segs = [{"start": i * 2.0, "end": (i + 1) * 2.0, "text": "x"} for i in range(4)]
        # With max_repeat=5, the 4-run is below threshold and not flagged
        r = check_quality(segs, config={"quality": {"max_repeat_segments": 5, "enabled": True}})
        self.assertFalse(any(i.code == ISSUE_REPEATED_SEGMENT for i in r.issues))

    def test_non_consecutive_repetition_not_flagged(self) -> None:
        # alternating pattern, never runs of >= 3
        segs = [
            {"start": 0.0, "end": 1.0, "text": "a"},
            {"start": 1.0, "end": 2.0, "text": "b"},
            {"start": 2.0, "end": 3.0, "text": "a"},
            {"start": 3.0, "end": 4.0, "text": "b"},
            {"start": 4.0, "end": 5.0, "text": "a"},
        ]
        r = check_quality(segs)
        self.assertFalse(any(i.code == ISSUE_REPEATED_SEGMENT for i in r.issues))


class TranslationTests(unittest.TestCase):
    def test_empty_translation_flagged(self) -> None:
        zh = _clean_zh()
        zh[1] = ""  # one empty translation
        r = check_quality(_clean_segs(), {"zh": zh})
        codes = [i.code for i in r.issues]
        self.assertIn(ISSUE_EMPTY_TRANSLATION, codes)

    def test_too_short_translation_flagged(self) -> None:
        zh = _clean_zh()
        zh[0] = "x"  # 1 char vs 27 char source → ratio 0.04
        r = check_quality(_clean_segs(), {"zh": zh})
        codes = [i.code for i in r.issues]
        self.assertIn(ISSUE_TRANSLATION_RATIO, codes)

    def test_too_long_translation_flagged(self) -> None:
        zh = _clean_zh()
        zh[0] = "x" * 200  # 200 chars vs 27 → ratio > 3
        r = check_quality(_clean_segs(), {"zh": zh})
        codes = [i.code for i in r.issues]
        self.assertIn(ISSUE_TRANSLATION_RATIO, codes)

    def test_length_mismatch_silently_skipped(self) -> None:
        # translations length doesn't match segments — not a quality issue,
        # the renderer would have raised. Just skip.
        r = check_quality(_clean_segs(), {"zh": ["only one line"]})
        codes = [i.code for i in r.issues]
        self.assertNotIn(ISSUE_EMPTY_TRANSLATION, codes)
        self.assertNotIn(ISSUE_TRANSLATION_RATIO, codes)


class TinySegmentCountTests(unittest.TestCase):
    def test_two_segments_flagged(self) -> None:
        segs = [
            {"start": 0.0, "end": 2.0, "text": "Hello world"},
            {"start": 2.0, "end": 4.0, "text": "Goodbye world"},
        ]
        r = check_quality(segs, {"zh": ["你好", "再见"]})
        self.assertTrue(any(i.code == ISSUE_TINY_SEGMENT_COUNT for i in r.issues))

    def test_five_segments_not_flagged(self) -> None:
        r = check_quality(_clean_segs())
        self.assertFalse(any(i.code == ISSUE_TINY_SEGMENT_COUNT for i in r.issues))


class ScoreFormulaTests(unittest.TestCase):
    def test_single_warning_deducts_8(self) -> None:
        # Use short segment as a controlled warning
        segs = _clean_segs() + [{"start": 11.0, "end": 11.2, "text": "ok"}]
        r = check_quality(segs, {"zh": _clean_zh() + ["好"]})
        # Only one warning expected
        warnings = [i for i in r.issues if i.severity == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(r.score, 92)

    def test_single_error_deducts_25(self) -> None:
        # 3 consecutive identical segments → 1 error (repetition)
        segs = [{"start": i * 2.0, "end": (i + 1) * 2.0, "text": "same"} for i in range(3)]
        r = check_quality(segs)
        errors = [i for i in r.issues if i.severity == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(r.score, 75)

    def test_score_clamps_to_zero(self) -> None:
        # Build a report with many errors — score must not go below 0
        segs = [{"start": i * 0.1, "end": (i + 1) * 0.1, "text": "same text"} for i in range(20)]
        segs += [{"start": 5.0, "end": 5.1, "text": "tiny"} for _ in range(50)]
        r = check_quality(segs)
        self.assertGreaterEqual(r.score, 0)
        self.assertLessEqual(r.score, 100)

    def test_suspect_threshold_configurable(self) -> None:
        segs = _clean_segs() + [{"start": 11.0, "end": 11.2, "text": "ok"}]
        # Default threshold 80 → score 92 is NOT suspect
        r = check_quality(segs, {"zh": _clean_zh() + ["好"]})
        self.assertFalse(r.is_suspect)
        # Lower threshold to 95 → score 92 IS suspect
        r2 = check_quality(segs, {"zh": _clean_zh() + ["好"]}, config={"quality": {"suspect_score_threshold": 95, "enabled": True}})
        self.assertTrue(r2.is_suspect)


class IsSuspectHelperTests(unittest.TestCase):
    def test_none_is_not_suspect(self) -> None:
        self.assertFalse(is_suspect_report(None))

    def test_dict_shape(self) -> None:
        self.assertTrue(is_suspect_report({"is_suspect": True}))
        self.assertFalse(is_suspect_report({"is_suspect": False}))
        self.assertFalse(is_suspect_report({}))

    def test_dataclass_shape(self) -> None:
        from app.quality_checker import QualityReport
        self.assertTrue(is_suspect_report(QualityReport(score=50, is_suspect=True)))


class ToDictTests(unittest.TestCase):
    def test_round_trip_json_safe(self) -> None:
        r = check_quality(_clean_segs(), {"zh": _clean_zh()})
        d = r.to_dict()
        # Must be JSON-serializable (no dataclass leakage)
        json.dumps(d)
        self.assertIn("score", d)
        self.assertIn("issues", d)
        self.assertIn("is_suspect", d)
        self.assertIn("summary", d)
        self.assertIn("suspect_segment_ids", d)
        for issue in d["issues"]:
            self.assertIn("code", issue)
            self.assertIn("severity", issue)
            self.assertIn("message", issue)
            self.assertIn("segment_index", issue)
            self.assertIn("detail", issue)


class SuspectSegmentIdsTests(unittest.TestCase):
    def test_aggregates_unique_segment_indices(self) -> None:
        # Two distinct issues pointing at the same segment
        segs = [
            {"start": 0.0, "end": 0.3, "text": "hi"},       # too short
            {"start": 1.0, "end": 3.0, "text": "normal"},
            {"start": 3.0, "end": 5.0, "text": "normal"},
        ]
        r = check_quality(segs, {"zh": ["a", "b", "c"]})  # all short ratio
        # Multiple issues on segment 0 should be deduped to a single id
        self.assertIn(0, r.suspect_segment_ids)
        # No duplicate values
        self.assertEqual(len(r.suspect_segment_ids), len(set(r.suspect_segment_ids)))

    def test_includes_indices_from_low_confidence_segment_flag(self) -> None:
        # If a future check ever sets segment_index on low_confidence, it should show up.
        # Today low_confidence is aggregate (None index) — verify this contract.
        r = check_quality(_clean_segs(), asr_segments=[
            {"start": 0.0, "end": 2.0, "text": "x", "score": 0.1},
        ])
        low_conf_issues = [i for i in r.issues if i.code == ISSUE_LOW_CONFIDENCE]
        self.assertEqual(len(low_conf_issues), 1)
        self.assertIsNone(low_conf_issues[0].segment_index)


if __name__ == "__main__":
    unittest.main()
