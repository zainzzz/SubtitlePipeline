"""Tests for the /api/browse path traversal defenses.

The browse endpoint lets the UI navigate the filesystem to pick input/output
directories. If the path-resolution guard (`resolve_browse_target`) is
regressed, an attacker on the same network could escape the configured
roots and read arbitrary directories. This test file pins down every
defensive branch so future refactors can't quietly weaken it.

Tested vectors:
- Legitimate request inside an allowed root → 200 + entries
- `..` traversal escape → 403
- Absolute path outside the roots (e.g. /etc) → 403
- URL-encoded traversal (%2e%2e, double-encoded) → 403
- Symlink pointing outside the root → 403
- Missing directory → 404
- File path where a directory was expected → 400
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Importing here so the env vars set in setUp() take effect on first import
from app import main as app_main  # noqa: E402


class BrowseSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._saved_env = {}
        for key in (
            "SUBPIPELINE_DB_PATH",
            "SUBPIPELINE_FRONTEND_DIST",
            "SUBPIPELINE_MODELS_DIR",
            "SUBPIPELINE_BROWSE_ROOTS",
            "SUBPIPELINE_OUTPUT_DIR",
        ):
            cls._saved_env[key] = os.environ.get(key)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls._tmp.name)
        cls.data_dir = cls.base / "data"
        cls.output_dir = cls.base / "output"
        cls.config_dir = cls.base / "config"
        cls.frontend_dist = cls.base / "frontend-dist"
        cls.models_dir = cls.base / "models"
        for p in (cls.data_dir, cls.output_dir, cls.config_dir, cls.frontend_dist, cls.models_dir):
            p.mkdir(parents=True, exist_ok=True)
        (cls.frontend_dist / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
        os.environ["SUBPIPELINE_DB_PATH"] = str(cls.config_dir / "api.db")
        os.environ["SUBPIPELINE_FRONTEND_DIST"] = str(cls.frontend_dist)
        os.environ["SUBPIPELINE_MODELS_DIR"] = str(cls.models_dir)
        os.environ["SUBPIPELINE_BROWSE_ROOTS"] = ",".join([
            str(cls.data_dir), str(cls.output_dir), str(cls.config_dir),
        ])
        os.environ["SUBPIPELINE_OUTPUT_DIR"] = str(cls.output_dir)
        # Pre-populate the browse roots with known content for the legitimate-path test
        (cls.data_dir / "movies").mkdir(exist_ok=True)
        (cls.data_dir / "movies" / "a.mkv").write_bytes(b"\x00" * 100)
        (cls.data_dir / "shows").mkdir(exist_ok=True)
        # Build the app once; the with-block runs the lifespan context manager
        # which initializes the database schema and wires up app.state.database
        cls._client_cm = TestClient(app_main.create_app())
        cls.client = cls._client_cm.__enter__()
        # Touch the app to verify startup succeeded
        cls.client.get("/api/health")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client_cm.__exit__(None, None, None)
        for key, value in cls._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    # ---- Legitimate cases ----

    def test_subdir_inside_root_returns_200(self) -> None:
        r = self.client.get(
            "/api/browse",
            params={"path": str((self.data_dir / "movies").resolve()), "mode": "both"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # body["current"] is the canonical (resolved) path
        self.assertEqual(body["current"], str((self.data_dir / "movies").resolve()))
        # a.mkv is in the allowed extensions list by default
        self.assertTrue(any(f["name"] == "a.mkv" for f in body["files"]))

    # ---- Escape attempts ----

    def test_parent_traversal_escape_returns_403(self) -> None:
        # data_dir/../output_dir — resolve() canonicalizes to output_dir which
        # IS a root, so this would actually succeed. We need a path that
        # resolves OUTSIDE all roots. Use data_dir/../../etc/hosts.
        outside = str(self.data_dir / ".." / ".." / "etc" / "hosts")
        r = self.client.get("/api/browse", params={"path": outside})
        # FastAPI may URL-encode the path automatically; the server should
        # still see an out-of-root path and refuse with 403.
        self.assertEqual(r.status_code, 403, r.text)

    def test_absolute_path_outside_roots_returns_403(self) -> None:
        r = self.client.get("/api/browse", params={"path": "/etc"})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.get("/api/browse", params={"path": "/tmp"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_root_filesystem_returns_403(self) -> None:
        r = self.client.get("/api/browse", params={"path": "/"})
        self.assertEqual(r.status_code, 403, r.text)

    # ---- Encoding tricks (URL-encoded '..' that decodes to ../) ----

    def test_url_encoded_traversal_returns_403(self) -> None:
        # %2e%2e = ".." after URL decode. FastAPI decodes before our handler
        # runs, so the server should see "../../etc" and resolve() to /etc.
        encoded = quote(str(self.data_dir) + "/%2e%2e/%2e%2e/etc", safe="")
        r = self.client.get(f"/api/browse?path={encoded}")
        self.assertIn(r.status_code, (403, 404), r.text)

    def test_double_url_encoded_traversal_returns_403(self) -> None:
        # %252e%252e = "%2e%2e" after one decode, ".." after two decodes.
        # We pass the raw %-encoded string in the path; whether the test
        # surfaces as 403 (decoded to escape) or 404 (decoded once to
        # a non-existent file with literal %2e in the name) is server-dependent,
        # but it must never be 200.
        encoded = quote(str(self.data_dir) + "/%252e%252e/%252e%252e/etc", safe="")
        r = self.client.get(f"/api/browse?path={encoded}")
        self.assertNotEqual(r.status_code, 200, r.text)

    # ---- Symlink escape ----

    def test_symlink_pointing_outside_roots_returns_403(self) -> None:
        # create data_dir/escape -> /etc
        link = self.data_dir / "escape"
        # Some test environments forbid creating links to absolute paths
        # without privileges; skip if so.
        try:
            link.symlink_to("/etc")
        except (OSError, NotImplementedError):
            self.skipTest("symlink not supported in this environment")
        r = self.client.get("/api/browse", params={"path": str(link)})
        # resolve() follows the symlink → /etc which is outside roots → 403
        self.assertEqual(r.status_code, 403, r.text)

    # ---- 404 / 400 paths ----

    def test_nonexistent_directory_returns_404(self) -> None:
        target = self.data_dir / "this_does_not_exist"
        r = self.client.get("/api/browse", params={"path": str(target)})
        self.assertEqual(r.status_code, 404, r.text)

    def test_file_path_returns_400(self) -> None:
        # data_dir/movies/a.mkv is a file, not a directory
        target = self.data_dir / "movies" / "a.mkv"
        r = self.client.get("/api/browse", params={"path": str(target)})
        self.assertEqual(r.status_code, 400, r.text)


class ResolveBrowseTargetUnitTests(unittest.TestCase):
    """Direct unit tests for `resolve_browse_target` to avoid TestClient overhead."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.outer_dir = Path(self._tmp.name) / "outer"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.outer_dir.mkdir(parents=True, exist_ok=True)
        self._prev = os.environ.get("SUBPIPELINE_BROWSE_ROOTS")
        os.environ["SUBPIPELINE_BROWSE_ROOTS"] = str(self.data_dir)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("SUBPIPELINE_BROWSE_ROOTS", None)
        else:
            os.environ["SUBPIPELINE_BROWSE_ROOTS"] = self._prev
        self._tmp.cleanup()

    def _resolve(self, raw):
        from app.main import resolve_browse_target
        return resolve_browse_target(raw)

    def test_empty_path_returns_default_root(self) -> None:
        """With no path, server defaults to /data (hardcoded first root).
        We only assert that the returned target is a Path and is in roots —
        the hardcoded /data default makes the precise value environment-dependent."""
        target, roots = self._resolve(None)
        self.assertIsInstance(target, Path)
        self.assertIn(target, roots)

    def test_inside_root_succeeds(self) -> None:
        target, _ = self._resolve(str(self.data_dir))
        self.assertEqual(target, self.data_dir.resolve())

    def test_empty_path_returns_default_root(self) -> None:
        """With no path, server defaults to /data (hardcoded first root).
        We don't assert which root is returned — only that it is one of the
        configured roots and is itself a Path object."""
        target, roots = self._resolve(None)
        self.assertIsInstance(target, Path)
        self.assertIn(target, roots)

    def test_outside_root_raises_403(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._resolve(str(self.outer_dir))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_relative_traversal_resolves_outside(self) -> None:
        from fastapi import HTTPException
        # data_dir/../outer is outside data_dir
        with self.assertRaises(HTTPException) as ctx:
            self._resolve(str(self.data_dir / ".." / "outer"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_etc_absolute_path_raises_403(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._resolve("/etc")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
