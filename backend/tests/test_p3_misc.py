from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline import (
    TaskContext,
    _required_resume_files,
    check_resume_feasibility,
)
from app.store import Database


class _PipelineFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_dir = self.base / "config"
        self.data_dir = self.base / "data"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = Database(str(self.config_dir / "test.db"))
        self.database.initialize()
        self.database.update_config(
            {
                "file": {
                    "input_dir": str(self.data_dir),
                    "output_to_source_dir": False,
                    "min_size_mb": 0,
                    "allowed_extensions": [".mp4"],
                },
                "processing": {
                    "work_dir": str(self.config_dir / "work"),
                    "max_retries": 2,
                    "retry_mode": "resume",
                },
                "translation": {
                    "enabled": False,
                    "target_languages": ["zh-CN"],
                    "max_retries": 1,
                    "api_base_url": "",
                    "api_key": "",
                    "model": "",
                    "llm_type": "openai-compatible",
                },
                "subtitle": {
                    "bilingual": False,
                    "bilingual_mode": "merge",
                    "filename_template": "{stem}.{lang}.srt",
                },
                "mux": {"enabled": False, "filename_template": "{stem}.mkv"},
            }
        )
        config = self.database.get_config()
        self.context = TaskContext(
            task_id=1,
            file_path="/fake/video.mp4",
            config_snapshot=config,
            work_dir=self.config_dir / "work" / "1",
        )
        self.context.work_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()


class RequiredResumeFilesSignatureTests(_PipelineFixture):
    """P3-1: _required_resume_files takes stage: str, not task: dict."""

    def test_accepts_string_stage_argument(self) -> None:
        files = _required_resume_files("translate", self.context)
        self.assertIsInstance(files, list)

    def test_early_stage_requires_fewer_files(self) -> None:
        early = _required_resume_files("run_asr", self.context)
        late = _required_resume_files("subtitle_render", self.context)
        self.assertLess(len(early), len(late))

    def test_mux_stage_includes_artifacts(self) -> None:
        files = _required_resume_files("mux", self.context)
        names = [name for name, _ in files]
        self.assertIn("artifacts.json", names)

    def test_check_resume_feasibility_still_works(self) -> None:
        task = {
            "id": 1,
            "file_path": "/fake/video.mp4",
            "stage": "translate",
            "config_snapshot": self.database.get_config(),
        }
        result = check_resume_feasibility(task)
        self.assertIn("can_resume", result)
        self.assertIn("missing", result)
        self.assertIn("resume_stage", result)
        self.assertIsInstance(result["missing"], list)


class DebugTranslationRequestLivenessTests(unittest.TestCase):
    """P3-3: Verify debug_translation_request is NOT dead code.

    It is actively imported by backend/debug_translation.py (a CLI tool) and
    tested in backend/tests/test_mvp.py.
    """

    def test_function_is_importable_from_pipeline(self) -> None:
        from app.pipeline import debug_translation_request

        self.assertTrue(callable(debug_translation_request))

    def test_debug_translation_cli_imports_it(self) -> None:
        cli_path = ROOT / "debug_translation.py"
        self.assertTrue(cli_path.exists(), "debug_translation.py CLI should exist")
        content = cli_path.read_text(encoding="utf-8")
        self.assertIn("debug_translation_request", content)

    def test_test_mvp_imports_it(self) -> None:
        test_path = ROOT / "tests" / "test_mvp.py"
        self.assertTrue(test_path.exists())
        content = test_path.read_text(encoding="utf-8")
        self.assertIn("debug_translation_request", content)


class RunAsrTypeAnnotationTests(unittest.TestCase):
    """P3-2: run_asr should accept TaskContext, not Any."""

    def test_run_asr_signature_uses_task_context(self) -> None:
        import inspect
        from app.asr.service import run_asr

        sig = inspect.signature(run_asr)
        context_param = sig.parameters["context"]
        annotation_str = str(context_param.annotation)
        self.assertIn("TaskContext", annotation_str)
        self.assertNotEqual(annotation_str, "Any")


if __name__ == "__main__":
    unittest.main()
