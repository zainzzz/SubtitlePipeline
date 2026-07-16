from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import Database


class _ConfigFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_dir = self.base / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.database = Database(str(self.config_dir / "test.db"))
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()


class ConfigCacheVersionTests(_ConfigFixture):
    """P1-6: Config cache must invalidate across processes via version check."""

    def test_get_config_returns_updated_values_after_update(self) -> None:
        original = self.database.get_config()
        self.database.update_config({"file": {"min_size_mb": 256}})
        updated = self.database.get_config()
        self.assertEqual(updated["file"]["min_size_mb"], 256)
        self.assertNotEqual(original["file"]["min_size_mb"], 256)

    def test_config_version_is_bumped_on_update(self) -> None:
        version_before = self.database._get_config_version()
        self.database.update_config({"file": {"min_size_mb": 512}})
        version_after = self.database._get_config_version()
        self.assertGreater(version_after, version_before)

    def test_version_mismatch_triggers_reload(self) -> None:
        self.database.get_config()
        self.assertIsNotNone(self.database._cached_config_version)
        self.database.update_config({"file": {"min_size_mb": 999}})
        old_cached_version = self.database._cached_config_version
        self.database._cached_config_version = -999
        config = self.database.get_config()
        self.assertEqual(config["file"]["min_size_mb"], 999)
        self.assertNotEqual(self.database._cached_config_version, -999)

    def test_rapid_consecutive_updates_dont_lose_data(self) -> None:
        for i in range(10):
            self.database.update_config({"file": {"min_size_mb": i * 10}})
        final = self.database.get_config()
        self.assertEqual(final["file"]["min_size_mb"], 90)

    def test_cross_process_simulation_via_new_database_instance(self) -> None:
        db1 = self.database
        db1.update_config({"file": {"min_size_mb": 42}})
        cached_val = db1.get_config()["file"]["min_size_mb"]
        self.assertEqual(cached_val, 42)

        db2 = Database(str(self.config_dir / "test.db"))
        db2.initialize()
        config2 = db2.get_config()
        self.assertEqual(config2["file"]["min_size_mb"], 42)

        db1.update_config({"file": {"min_size_mb": 99}})
        config2_again = db2.get_config()
        self.assertEqual(config2_again["file"]["min_size_mb"], 99)
        db2.close()


if __name__ == "__main__":
    unittest.main()
