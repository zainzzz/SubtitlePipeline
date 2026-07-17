"""P2-9: Verify that translation.api_key is redacted in /api/config responses.

Tests that the GET /api/config and PUT /api/config endpoints never leak the
real API key, while still accepting the real key on write.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from app.main import _redact_config, create_app
from app.store import Database

SECRET_KEY = "sk-test-secret-do-not-leak-12345"


class RedactHelperTests(unittest.TestCase):
    """Unit tests for the _redact_config helper function."""

    def test_redacts_api_key_in_nested_dict(self) -> None:
        config = {
            "translation": {
                "enabled": True,
                "api_key": SECRET_KEY,
                "model": "gpt-4o-mini",
            }
        }
        result = _redact_config(config)
        self.assertEqual(result["translation"]["api_key"], "***")
        self.assertEqual(result["translation"]["model"], "gpt-4o-mini")

    def test_preserves_empty_api_key(self) -> None:
        config = {"translation": {"api_key": "", "model": "gpt-4o-mini"}}
        result = _redact_config(config)
        self.assertEqual(result["translation"]["api_key"], "")

    def test_preserves_none_api_key(self) -> None:
        config = {"translation": {"api_key": None}}
        result = _redact_config(config)
        self.assertIsNone(result["translation"]["api_key"])

    def test_does_not_mutate_original(self) -> None:
        config = {"translation": {"api_key": SECRET_KEY}}
        _redact_config(config)
        self.assertEqual(config["translation"]["api_key"], SECRET_KEY)

    def test_redacts_other_sensitive_keys(self) -> None:
        config = {
            "translation": {"api_key": SECRET_KEY, "password": "pw123", "token": "tok456"},
            "file": {"input_dir": "/data"},
        }
        result = _redact_config(config)
        self.assertEqual(result["translation"]["api_key"], "***")
        self.assertEqual(result["translation"]["password"], "***")
        self.assertEqual(result["translation"]["token"], "***")
        self.assertEqual(result["file"]["input_dir"], "/data")

    def test_redacts_recursively_in_lists(self) -> None:
        config = {"items": [{"api_key": SECRET_KEY}, {"name": "ok"}]}
        result = _redact_config(config)
        self.assertEqual(result["items"][0]["api_key"], "***")
        self.assertEqual(result["items"][1]["name"], "ok")


class ConfigEndpointRedactionTests(unittest.TestCase):
    """Integration tests using FastAPI TestClient."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_dir = self.base / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        os.environ["SUBPIPELINE_DB_PATH"] = str(self.config_dir / "test.db")
        os.environ["SUBPIPELINE_FRONTEND_DIST"] = str(self.base / "frontend-dist")
        os.environ["SUBPIPELINE_MODELS_DIR"] = str(self.base / "models")

        self.database = Database(str(self.config_dir / "test.db"))
        self.database.initialize()
        self.database.update_config({"translation": {"api_key": SECRET_KEY}})

        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def test_get_config_redacts_api_key(self) -> None:
        with self.client as client:
            response = client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["translation"]["api_key"], "***")
        self.assertNotIn(SECRET_KEY, response.text)

    def test_get_config_preserves_other_translation_fields(self) -> None:
        with self.client as client:
            response = client.get("/api/config")
        body = response.json()
        self.assertIn("model", body["translation"])
        self.assertIn("enabled", body["translation"])
        self.assertIn("api_base_url", body["translation"])

    def test_put_config_accepts_real_api_key(self) -> None:
        new_key = "sk-brand-new-key-67890"
        with self.client as client:
            response = client.put(
                "/api/config",
                json={"translation": {"api_key": new_key}},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["translation"]["api_key"], "***")

        stored = self.database.get_config()
        self.assertEqual(stored["translation"]["api_key"], new_key)

    def test_get_config_empty_key_when_not_set(self) -> None:
        self.database.update_config({"translation": {"api_key": ""}})
        with self.client as client:
            response = client.get("/api/config")
        body = response.json()
        self.assertEqual(body["translation"]["api_key"], "")


if __name__ == "__main__":
    unittest.main()
