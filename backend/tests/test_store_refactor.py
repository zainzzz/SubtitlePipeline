"""Tests for the extracted ``DatabaseMigrations`` and ``ConfigService`` classes.

These tests pin the post-refactor contracts of the classes extracted from the
monolithic ``Database`` (see ``backend/app/store/__init__.py`` for the facade
and ``backend/app/store/{migrations,config}.py`` for the implementations).

The tests intentionally mirror the fixtures used by ``test_p1_6_config_cache``,
``test_p1_7_ensure_column``, and ``_gap_skeletons/test_coverage_gaps.py`` so
the same bootstrap path is exercised. They run in Docker via::

    docker compose exec subpipeline python3 -m unittest backend.tests.test_store_refactor
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import Database  # noqa: E402
from app.store.config import ConfigService, _migrate_whisper_config_dict  # noqa: E402
from app.store.migrations import DatabaseMigrations  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture (mirrors _DatabaseFixture from _gap_skeletons/test_coverage_gaps.py)
# ---------------------------------------------------------------------------


class _StoreFixture(unittest.TestCase):
    """Bootstrap a real Database against a temp dir for behavioural tests.

    Uses the same shape as ``test_mvp.SubtitlePipelineMvpTests`` and the
    ``_DatabaseFixture`` in ``_gap_skeletons/test_coverage_gaps.py`` so the
    config-dependent code paths (claim/mark_done/etc.) work end-to-end.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_dir = self.base / "config"
        self.data_dir = self.base / "data"
        self.work_dir = self.config_dir / "work"
        for path in (self.config_dir, self.data_dir, self.work_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.database = Database(str(self.config_dir / "refactor.db"))
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()


# ---------------------------------------------------------------------------
# DatabaseMigrations
# ---------------------------------------------------------------------------


class DatabaseMigrationsInitializeTests(_StoreFixture):
    """1. ``DatabaseMigrations.initialize`` creates all expected tables."""

    EXPECTED_TABLES = {
        "files",
        "tasks",
        "task_logs",
        "system_config",
        "translation_cache",
        "scan_status",
    }

    def test_initialize_creates_all_expected_tables(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        table_names = {row["name"] for row in rows}
        missing = self.EXPECTED_TABLES - table_names
        self.assertEqual(
            missing,
            set(),
            f"initialize() should create all expected tables; missing: {missing}",
        )

    def test_initialize_is_idempotent(self) -> None:
        """Calling initialize() twice on the same DB must not raise."""
        # The fixture already called initialize() once in setUp; call again.
        self.database.initialize()  # should not raise
        # Sanity check: tables still present.
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS c FROM system_config").fetchone()
        self.assertGreaterEqual(row["c"], 1)


class DatabaseMigrationsEnsureColumnTests(_StoreFixture):
    """2–5. ``_ensure_column`` allowlist/regex/idempotency contracts."""

    def test_ensure_column_is_idempotent(self) -> None:
        """2. Adding the same column twice results in exactly one column."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        try:
            self.database.migrations._ensure_column(conn, "tasks", "dup_col", "TEXT")
            self.database.migrations._ensure_column(conn, "tasks", "dup_col", "TEXT")
            cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            self.assertEqual(cols.count("dup_col"), 1)
        finally:
            conn.close()

    def test_ensure_column_rejects_invalid_table_name(self) -> None:
        """3. Table name must be in the allowlist (SQL injection guard)."""
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(ValueError):
                self.database.migrations._ensure_column(
                    conn,
                    "evil_table; DROP TABLE tasks",
                    "col",
                    "TEXT",
                )
        finally:
            conn.close()

    def test_ensure_column_rejects_invalid_column_name(self) -> None:
        """4. Column name must match ``^[A-Za-z_][A-Za-z0-9_]*$``."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        try:
            with self.assertRaises(ValueError):
                self.database.migrations._ensure_column(
                    conn,
                    "tasks",
                    "col; DROP TABLE tasks",
                    "TEXT",
                )
            # Sanity: tasks table still exists after the rejection.
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchall()
            self.assertEqual(len(tables), 1)
        finally:
            conn.close()

    def test_ensure_column_rejects_invalid_definition(self) -> None:
        """5. Definition must match ``^[A-Za-z0-9 ()'_]*$`` (no semicolons etc.)."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        try:
            with self.assertRaises(ValueError):
                self.database.migrations._ensure_column(
                    conn,
                    "tasks",
                    "evil_col",
                    "TEXT; DROP TABLE tasks",
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# ConfigService
# ---------------------------------------------------------------------------


class ConfigServiceGetConfigTests(_StoreFixture):
    """6. ``ConfigService.get_config`` returns defaults when system_config empty."""

    def test_get_config_returns_defaults_when_system_config_empty(self) -> None:
        """With only the migrations-seeded defaults, get_config returns the
        default config dict shape — every default group is present."""
        # The fixture calls initialize() which seeds defaults, so this verifies
        # the read path merges DB rows onto defaults correctly.
        config = self.database.config_service.get_config()
        for required_group in ("file", "whisper", "translation", "subtitle", "mux", "processing"):
            self.assertIn(
                required_group,
                config,
                f"get_config() must include default group {required_group!r}",
            )
        # The synthetic meta bucket should also be present.
        self.assertIn("meta", config)
        self.assertIn("restart_required", config["meta"])

    def test_get_config_resolves_auto_device(self) -> None:
        """``whisper.device == 'auto'`` is resolved to a concrete device on read."""
        config = self.database.config_service.get_config()
        self.assertNotEqual(config["whisper"]["device"], "auto")


class ConfigServiceUpdateConfigTests(_StoreFixture):
    """7. ``ConfigService.update_config`` persists and bumps version."""

    def test_update_config_persists_and_bumps_version(self) -> None:
        version_before = self.database.config_service._get_config_version()
        self.database.config_service.update_config({"file": {"min_size_mb": 256}})
        version_after = self.database.config_service._get_config_version()
        self.assertGreater(version_after, version_before, "version must be bumped on update")

        # Persisted value is visible through a fresh ConfigService reading the same DB.
        fresh = ConfigService(self.database)
        self.assertEqual(fresh.get_config()["file"]["min_size_mb"], 256)

    def test_update_config_rejects_unknown_field(self) -> None:
        """Unknown fields raise KeyError to catch frontend typos."""
        with self.assertRaises(KeyError):
            self.database.config_service.update_config({"file": {"nonexistent_key": 1}})


class ConfigServiceCacheInvalidationTests(_StoreFixture):
    """8. Cross-process cache invalidation via the config_version row."""

    def test_cache_invalidates_when_version_mismatches_across_instances(self) -> None:
        """Two Database instances over the same file must observe each other's writes.

        Simulates a multi-process deployment (scanner/worker/api each have their
        own Database) — db1 writes, db2's cached copy must be discarded on the
        next get_config() because the version row changed.
        """
        db1 = self.database
        db1.config_service.update_config({"file": {"min_size_mb": 42}})
        # Prime db1's cache.
        self.assertEqual(db1.get_config()["file"]["min_size_mb"], 42)

        db2 = Database(str(self.config_dir / "refactor.db"))
        db2.initialize()
        try:
            self.assertEqual(db2.get_config()["file"]["min_size_mb"], 42)
            # db1 mutates; db2 must see it on next read.
            db1.config_service.update_config({"file": {"min_size_mb": 99}})
            self.assertEqual(db2.get_config()["file"]["min_size_mb"], 99)
        finally:
            db2.close()


class WhisperConfigMigrationTests(unittest.TestCase):
    """Bonus: pin the ``_migrate_whisper_config_dict`` helper contract."""

    def test_legacy_align_method_is_normalized_to_align_provider(self) -> None:
        cfg = {"align_method": "simple"}
        _migrate_whisper_config_dict(cfg)
        self.assertEqual(cfg.get("align_provider"), "none")
        self.assertNotIn("align_method", cfg)

    def test_explicit_align_provider_is_clamped(self) -> None:
        cfg = {"align_provider": "bogus"}
        _migrate_whisper_config_dict(cfg)
        self.assertEqual(cfg["align_provider"], "auto")


# ---------------------------------------------------------------------------
# Database facade delegation
# ---------------------------------------------------------------------------


class DatabaseFacadeDelegationTests(_StoreFixture):
    """9–10. The slimmed Database delegates to its extracted services."""

    def test_database_initialize_calls_migrations_initialize(self) -> None:
        """10. ``Database.initialize()`` dispatches to ``self.migrations.initialize()``."""
        original = self.database.migrations.initialize
        called = {"count": 0}

        def spy() -> None:
            called["count"] += 1
            return original()

        self.database.migrations.initialize = spy  # type: ignore[method-assign]
        try:
            self.database.initialize()
        finally:
            self.database.migrations.initialize = original  # type: ignore[method-assign]
        self.assertEqual(called["count"], 1)

    def test_database_get_config_calls_config_service(self) -> None:
        """9. ``Database.get_config()`` reads through ``self.config_service``."""
        spy = MagicMock(wraps=self.database.config_service.get_config)
        self.database.config_service.get_config = spy  # type: ignore[method-assign]
        try:
            result = self.database.get_config()
        finally:
            spy.assert_called_once_with()
        self.assertIsInstance(result, dict)


class DatabaseBackwardCompatTests(_StoreFixture):
    """11–12. Every pre-refactor public method is still callable on Database."""

    def test_from_app_store_import_database_still_works(self) -> None:
        """11. The canonical import path resolves to the same class."""
        # Imported at top of file; sanity check the class identity.
        from app.store import Database as ImportedDatabase

        self.assertIs(ImportedDatabase, Database)
        self.assertIs(self.database.__class__, ImportedDatabase)

    def test_database_extracts_migrations_and_config_service_attributes(self) -> None:
        """The facade exposes the extracted services for advanced callers."""
        self.assertIsInstance(self.database.migrations, DatabaseMigrations)
        self.assertIsInstance(self.database.config_service, ConfigService)

    def test_database_public_lifecycle_methods_are_callable(self) -> None:
        """12. claim/mark_*/list_*/*_task* remain on Database and accept their args."""
        # We don't assert outcomes here — we only pin the public surface exists
        # with the right signature. Other test modules cover behaviour.
        for method_name in (
            "list_tasks",
            "status_counts",
            "count_tasks_by_status",
            "get_task",
            "get_logs",
            "log",
            "request_cancel",
            "request_retry",
            "observe_file",
            "observe_files_batch",
            "get_active_task_path_keys",
            "get_existing_task_versions",
            "has_active_task",
            "has_task_for_file_version",
            "create_task",
            "claim_next_pending_task",
            "update_task_stage",
            "mark_task_done",
            "mark_task_cancelled",
            "mark_task_failure",
            "delete_task",
            "is_cancel_requested",
            "get_scan_status",
            "record_scan_result",
            "get_translation_cache",
            "set_translation_cache",
            "recover_orphaned_tasks",
            "clear_restart_required",
            "is_setup_complete",
            "get_system_status",
            "set_setup_complete",
        ):
            self.assertTrue(
                callable(getattr(self.database, method_name)),
                f"Database.{method_name} must remain callable for backward compat",
            )

    def test_database_underscore_prefixed_helpers_still_delegate(self) -> None:
        """Pre-refactor underscore-prefixed helpers still work via delegation."""
        # _ensure_column
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id INTEGER)")
        try:
            self.database._ensure_column(conn, "tasks", "via_facade", "TEXT")
            cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
            self.assertIn("via_facade", cols)
        finally:
            conn.close()

        # _get_config_version + _bump_config_version
        v0 = self.database._get_config_version()
        with self.database.connect() as connection:
            v1 = self.database._bump_config_version(connection)
        self.assertGreater(v1, v0)

        # _get_config_uncached
        uncached = self.database._get_config_uncached()
        self.assertIsInstance(uncached, dict)
        self.assertIn("file", uncached)

    def test_database_cache_attribute_proxies_round_trip(self) -> None:
        """Cache attrs on Database are property proxies over ConfigService state."""
        # Write through Database.
        self.database._cached_config_version = 4242
        # Read through ConfigService.
        self.assertEqual(self.database.config_service._cached_config_version, 4242)
        # Write through ConfigService, read through Database.
        self.database.config_service._cache_valid = True
        self.assertIs(self.database._cache_valid, True)
        # Round-trip a cache dict.
        sentinel = {"sentinel": True}
        self.database._config_cache = sentinel
        self.assertIs(self.database.config_service._config_cache, sentinel)


if __name__ == "__main__":
    unittest.main()
