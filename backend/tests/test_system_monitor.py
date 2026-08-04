"""Tests for the pre-ASR resource check.

Covers:
- estimate_vram_mb: model table hits + prefix fallback + unknown default
- estimate_disk_mb: floor (1024 MB) for tiny files, scaled for larger ones
- get_gpu_free_mb: returns None when torch is unavailable / not on CUDA
- get_disk_free_mb: returns positive int on a real path, None on bad path
- check_resources: ok=True when both checks pass; ok=False with
  recommendations when either fails; skipped (ok=True, reasons explain)
  when pre_asr_resource_check is disabled
- is_sufficient helper handles None, dict, and dataclass shapes
- to_dict round-trip is JSON-safe
- Backward compat: the runtime wrapper _check_pre_asr_resources never
  blocks the worker when the check is disabled
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.system_monitor import (
    ResourceCheck,
    check_resources,
    estimate_disk_mb,
    estimate_vram_mb,
    get_disk_free_mb,
    get_gpu_free_mb,
    is_sufficient,
)


class EstimateVramTests(unittest.TestCase):
    def test_known_models(self) -> None:
        self.assertEqual(estimate_vram_mb("whisperx-tiny"), 1500)
        self.assertEqual(estimate_vram_mb("whisperx-small"), 2000)
        self.assertEqual(estimate_vram_mb("whisperx-large-v3"), 5000)
        self.assertEqual(estimate_vram_mb("faster-whisper-large-v3"), 4500)
        self.assertEqual(estimate_vram_mb("qwen3-asr-1.7b"), 4000)

    def test_unknown_model_falls_back_to_midrange(self) -> None:
        self.assertEqual(estimate_vram_mb("never-seen-model"), 3000)

    def test_empty_string_falls_back(self) -> None:
        self.assertEqual(estimate_vram_mb(""), 3000)

    def test_prefix_match(self) -> None:
        # "faster-whisper-base-anything" should match "faster-whisper-base"
        self.assertEqual(estimate_vram_mb("faster-whisper-base-extra"), 1500)

    def test_case_insensitive(self) -> None:
        self.assertEqual(estimate_vram_mb("WHISPERX-SMALL"), 2000)


class EstimateDiskTests(unittest.TestCase):
    def test_zero_audio_returns_floor(self) -> None:
        self.assertEqual(estimate_disk_mb(0), 1024)

    def test_tiny_audio_returns_floor(self) -> None:
        # 1 MB audio → 1.5 MB needed + 200 MB baseline, max with 1024 floor
        self.assertEqual(estimate_disk_mb(1024 * 1024), max(1024, int(1 * 1.5) + 200))

    def test_large_audio_scales(self) -> None:
        # 10 GB audio → ~15 GB needed
        ten_gb = 10 * 1024 * 1024 * 1024
        estimate = estimate_disk_mb(ten_gb)
        self.assertGreater(estimate, 5 * 1024)  # > 5 GB
        self.assertLess(estimate, 50 * 1024)    # < 50 GB

    def test_negative_audio_treated_as_zero(self) -> None:
        self.assertEqual(estimate_disk_mb(-1), 1024)


class GetGpuFreeTests(unittest.TestCase):
    def test_returns_none_without_torch(self) -> None:
        # No torch installed in the test env (or torch but no CUDA)
        # Either way the function should return None gracefully
        result = get_gpu_free_mb()
        if result is not None:
            # If a CUDA-capable torch IS installed, just sanity-check the value
            self.assertGreater(result, 0)
        else:
            self.assertIsNone(result)

    def test_returns_none_on_import_error(self) -> None:
        """Simulate torch import failure → returns None, not raises."""
        import builtins

        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("simulated torch absence")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            self.assertIsNone(get_gpu_free_mb())


class GetDiskFreeTests(unittest.TestCase):
    def test_returns_positive_int_on_real_path(self) -> None:
        free = get_disk_free_mb("/tmp")
        self.assertIsNotNone(free)
        self.assertGreater(free, 0)

    def test_returns_none_on_bad_path(self) -> None:
        self.assertIsNone(get_disk_free_mb("/this/path/does/not/exist/at/all"))


class CheckResourcesTests(unittest.TestCase):
    def test_disabled_returns_ok_with_disabled_reason(self) -> None:
        check = check_resources(
            "whisperx-small", 100 * 1024 * 1024, "/tmp",
            config={"processing": {"pre_asr_resource_check": False}},
        )
        self.assertTrue(check.ok)
        self.assertEqual(check.gpu_reason, "check disabled")
        self.assertEqual(check.disk_reason, "check disabled")

    def test_passes_when_no_gpu_and_disk_ok(self) -> None:
        # Test env likely has no CUDA — gpu_available_mb is None → no fail
        check = check_resources("whisperx-small", 100 * 1024 * 1024, "/tmp")
        self.assertTrue(check.ok)
        self.assertIn("未检测到 GPU", check.gpu_reason)

    def test_fails_when_disk_insufficient(self) -> None:
        """Pretend the disk has only 100 MB free — well below the 1.5 GB estimate."""
        with patch("app.system_monitor.get_disk_free_mb", return_value=100):
            check = check_resources(
                "whisperx-small", 10 * 1024 * 1024 * 1024, "/tmp",
                config={"processing": {"pre_asr_resource_check": True, "resource_headroom_pct": 0}},
            )
        self.assertFalse(check.ok)
        self.assertLess(check.disk_available_mb, check.disk_required_mb)
        # Recommendations are present
        self.assertGreater(len(check.recommendations), 0)
        # Each recommendation is a non-empty string
        for rec in check.recommendations:
            self.assertIsInstance(rec, str)
            self.assertGreater(len(rec.strip()), 0)

    def test_fails_when_gpu_insufficient(self) -> None:
        """Pretend GPU has only 500 MB free."""
        with patch("app.system_monitor.get_gpu_free_mb", return_value=500):
            check = check_resources(
                "whisperx-large-v3", 100 * 1024 * 1024, "/tmp",
                config={"processing": {"pre_asr_resource_check": True, "resource_headroom_pct": 0}},
            )
        self.assertFalse(check.ok)
        # GPU reason mentions memory
        self.assertIn("GPU 显存", check.gpu_reason)
        # Recommendations include a "switch to smaller model" hint
        self.assertTrue(any("切换" in r or "更小" in r for r in check.recommendations))

    def test_headroom_buffer_applied(self) -> None:
        """With headroom 50%, the effective required = required * 1.5.
        Pretend GPU has exactly the raw required — should fail because of buffer."""
        # whisperx-small = 2000 MB raw, +50% = 3000 MB
        with patch("app.system_monitor.get_gpu_free_mb", return_value=2000), \
             patch("app.system_monitor.get_disk_free_mb", return_value=10000):
            check = check_resources(
                "whisperx-small", 100 * 1024 * 1024, "/tmp",
                config={"processing": {"pre_asr_resource_check": True, "resource_headroom_pct": 50}},
            )
        self.assertFalse(check.ok)
        self.assertIn("3000", check.gpu_reason)

    def test_headroom_clamps_out_of_range(self) -> None:
        """headroom > 80 should clamp to 80, < 0 should clamp to 0 — but
        importantly the check still runs and produces a result."""
        for bad_headroom in (-10, 200, 999):
            check = check_resources(
                "whisperx-small", 100 * 1024 * 1024, "/tmp",
                config={"processing": {"pre_asr_resource_check": True, "resource_headroom_pct": bad_headroom}},
            )
            # The function does not raise; it produces a check object.
            self.assertIsInstance(check, ResourceCheck)


class IsSufficientHelperTests(unittest.TestCase):
    def test_none_is_sufficient(self) -> None:
        self.assertTrue(is_sufficient(None))

    def test_dict_shape(self) -> None:
        self.assertTrue(is_sufficient({"ok": True}))
        self.assertFalse(is_sufficient({"ok": False}))

    def test_dataclass_shape(self) -> None:
        self.assertTrue(is_sufficient(ResourceCheck(ok=True)))
        self.assertFalse(is_sufficient(ResourceCheck(ok=False)))


class ToDictTests(unittest.TestCase):
    def test_round_trip_json_safe(self) -> None:
        check = check_resources("whisperx-small", 100 * 1024 * 1024, "/tmp")
        d = check.to_dict()
        # Must be JSON-serializable
        json.dumps(d)
        self.assertIn("ok", d)
        self.assertIn("gpu_required_mb", d)
        self.assertIn("disk_required_mb", d)
        self.assertIn("recommendations", d)


class RuntimeIntegrationSmokeTests(unittest.TestCase):
    """Smoke test: when pre_asr_resource_check is False, the worker-side
    wrapper _check_pre_asr_resources should be a no-op even if the check
    would otherwise fail."""

    def test_disabled_check_is_noop_at_runtime(self) -> None:
        from app.runtime import WorkerService
        # Instantiate without going through WorkerService.__init__ (which
        # would try to load models). Just patch the bits we need.
        from app.store import Database
        import tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "t.db"), persistent=True)
            db.initialize()
            db.update_config({
                "file": {"input_dir": str(Path(tmp) / "m")},
                "processing": {
                    "work_dir": str(Path(tmp) / "w"),
                    "max_retries": 1,
                    "pre_asr_resource_check": False,
                },
            })
            ws = WorkerService.__new__(WorkerService)
            ws.database = db
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"\x00" * 100)
            # No exception even though check would otherwise run
            ws._check_pre_asr_resources(
                task={"id": 1, "file_path": str(audio)},
                snapshot={"processing": {"work_dir": str(Path(tmp) / "w")}, "whisper": {"model_name": "whisperx-large-v3"}},
                audio_path=audio,
            )


if __name__ == "__main__":
    unittest.main()
