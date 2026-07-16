from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import defaults


class TestCorsConfig(unittest.TestCase):
    def test_default_allowed_origins_constant_exists(self) -> None:
        self.assertTrue(hasattr(defaults, "DEFAULT_ALLOWED_ORIGINS"))

    def test_default_allowed_origins_is_list(self) -> None:
        self.assertIsInstance(defaults.DEFAULT_ALLOWED_ORIGINS, list)

    def test_default_does_not_allow_wildcard(self) -> None:
        self.assertNotIn("*", defaults.DEFAULT_ALLOWED_ORIGINS)

    def test_default_uses_localhost(self) -> None:
        self.assertIn("http://localhost:8000", defaults.DEFAULT_ALLOWED_ORIGINS)

    def test_get_allowed_origins_returns_default_when_env_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUBPIPELINE_ALLOWED_ORIGINS", None)
            defaults.get_allowed_origins.cache_clear()
            result = defaults.get_allowed_origins()
            self.assertEqual(result, list(defaults.DEFAULT_ALLOWED_ORIGINS))

    def test_get_allowed_origins_returns_default_when_env_empty(self) -> None:
        with patch.dict(os.environ, {"SUBPIPELINE_ALLOWED_ORIGINS": "  "}):
            defaults.get_allowed_origins.cache_clear()
            self.assertEqual(defaults.get_allowed_origins(), list(defaults.DEFAULT_ALLOWED_ORIGINS))

    def test_get_allowed_origins_parses_comma_separated(self) -> None:
        with patch.dict(os.environ, {"SUBPIPELINE_ALLOWED_ORIGINS": "https://a.test, http://b.test"}):
            defaults.get_allowed_origins.cache_clear()
            self.assertEqual(
                defaults.get_allowed_origins(),
                ["https://a.test", "http://b.test"],
            )

    def test_get_allowed_origins_ignores_empty_entries(self) -> None:
        with patch.dict(os.environ, {"SUBPIPELINE_ALLOWED_ORIGINS": "https://a.test,, ,http://b.test"}):
            defaults.get_allowed_origins.cache_clear()
            self.assertEqual(
                defaults.get_allowed_origins(),
                ["https://a.test", "http://b.test"],
            )


if __name__ == "__main__":
    unittest.main()
