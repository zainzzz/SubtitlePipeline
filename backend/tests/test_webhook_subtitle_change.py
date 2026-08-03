"""Tests for the subtitle-change webhook path.

Covers:
- `send_webhook_notification` skips when disabled or URL is missing
- `send_webhook_notification` correctly reports success / failure per provider
- `send_webhook_notification` carries the `triggered_by` argument through
- store._migrate_notification_config backfills new defaults on old config rows
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import _migrate_notification_config
from app.webhook import (
    TRIGGER_COMPLETION,
    TRIGGER_SUBTITLE_CHANGE,
    WEBHOOK_STATE_FAILED,
    WEBHOOK_STATE_SKIPPED,
    WEBHOOK_STATE_SUCCESS,
    send_webhook_notification,
)


def _make_mock_client(*, post_side_effect=None, get_side_effect=None):
    """Build a context-managed mock httpx.Client."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    if post_side_effect is not None:
        mock_client.post = MagicMock(side_effect=post_side_effect)
    if get_side_effect is not None:
        mock_client.get = MagicMock(side_effect=get_side_effect)
    return mock_client


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock(return_value=None)
    return resp


class SendWebhookNotificationTests(unittest.TestCase):
    def test_disabled_returns_skipped(self) -> None:
        status = send_webhook_notification(
            {"notification": {"webhook_enabled": False}},
            {"id": 1, "file_path": "/data/movie.mkv"},
        )
        self.assertEqual(status.state, WEBHOOK_STATE_SKIPPED)
        self.assertEqual(status.detail, "webhook_enabled is false")
        self.assertEqual(status.triggered_by, TRIGGER_COMPLETION)
        self.assertIsNone(status.webhook_type)

    def test_no_url_returns_skipped_with_trigger(self) -> None:
        status = send_webhook_notification(
            {"notification": {"webhook_enabled": True}},
            {"id": 2, "file_path": "/data/movie.mkv"},
            trigger=TRIGGER_SUBTITLE_CHANGE,
        )
        self.assertEqual(status.state, WEBHOOK_STATE_SKIPPED)
        self.assertEqual(status.detail, "webhook_url not set")
        self.assertEqual(status.triggered_by, TRIGGER_SUBTITLE_CHANGE)

    def test_generic_post_success(self) -> None:
        with patch("app.webhook.httpx.Client") as MC:
            MC.return_value = _make_mock_client(post_side_effect=_ok_response())
            status = send_webhook_notification(
                {
                    "notification": {
                        "webhook_enabled": True,
                        "webhook_type": "generic",
                        "webhook_url": "http://example.com/hook",
                        "webhook_token": "secret",
                    }
                },
                {"id": 7, "file_path": "/data/movie.mkv"},
                trigger=TRIGGER_SUBTITLE_CHANGE,
            )
        self.assertEqual(status.state, WEBHOOK_STATE_SUCCESS)
        self.assertEqual(status.webhook_type, "generic")
        self.assertEqual(status.triggered_by, TRIGGER_SUBTITLE_CHANGE)
        self.assertIsNone(status.error)

    def test_jellyfin_post_success(self) -> None:
        with patch("app.webhook.httpx.Client") as MC:
            MC.return_value = _make_mock_client(post_side_effect=_ok_response())
            status = send_webhook_notification(
                {
                    "notification": {
                        "webhook_enabled": True,
                        "webhook_type": "jellyfin",
                        "webhook_url": "http://jellyfin.local:8096",
                        "webhook_token": "abc",
                        "webhook_library_id": "lib1,lib2",
                    }
                },
                {"id": 8, "file_path": "/data/movies/foo.mkv"},
            )
        self.assertEqual(status.state, WEBHOOK_STATE_SUCCESS)
        self.assertEqual(status.webhook_type, "jellyfin")

    def test_emby_post_success(self) -> None:
        with patch("app.webhook.httpx.Client") as MC:
            MC.return_value = _make_mock_client(post_side_effect=_ok_response())
            status = send_webhook_notification(
                {
                    "notification": {
                        "webhook_enabled": True,
                        "webhook_type": "emby",
                        "webhook_url": "http://emby.local:8096",
                        "webhook_token": "abc",
                    }
                },
                {"id": 9, "file_path": "/data/movies/foo.mkv"},
                trigger=TRIGGER_SUBTITLE_CHANGE,
            )
        self.assertEqual(status.state, WEBHOOK_STATE_SUCCESS)
        self.assertEqual(status.webhook_type, "emby")
        self.assertEqual(status.triggered_by, TRIGGER_SUBTITLE_CHANGE)

    def test_plex_uses_get(self) -> None:
        with patch("app.webhook.httpx.Client") as MC:
            MC.return_value = _make_mock_client(get_side_effect=_ok_response())
            status = send_webhook_notification(
                {
                    "notification": {
                        "webhook_enabled": True,
                        "webhook_type": "plex",
                        "webhook_url": "http://plex.local:32400",
                        "webhook_token": "plextoken",
                    }
                },
                {"id": 10, "file_path": "/data/movies/foo.mkv"},
            )
        self.assertEqual(status.state, WEBHOOK_STATE_SUCCESS)
        self.assertEqual(status.webhook_type, "plex")

    def test_5xx_is_reported_as_failed(self) -> None:
        boom = Exception("500 Server Error: connection refused")
        with patch("app.webhook.httpx.Client") as MC:
            MC.return_value = _make_mock_client(post_side_effect=boom)
            status = send_webhook_notification(
                {
                    "notification": {
                        "webhook_enabled": True,
                        "webhook_type": "emby",
                        "webhook_url": "http://emby.invalid:8096",
                        "webhook_token": "x",
                    }
                },
                {"id": 11, "file_path": "/data/x.mkv"},
                trigger=TRIGGER_SUBTITLE_CHANGE,
            )
        self.assertEqual(status.state, WEBHOOK_STATE_FAILED)
        self.assertEqual(status.webhook_type, "emby")
        self.assertIn("500", status.error or "")
        self.assertEqual(status.triggered_by, TRIGGER_SUBTITLE_CHANGE)

    def test_default_trigger_is_completion(self) -> None:
        """Calls without `trigger` argument must default to TRIGGER_COMPLETION
        (backward compatibility for worker.py's existing call site)."""
        status = send_webhook_notification(
            {"notification": {"webhook_enabled": False}},
            {"id": 12, "file_path": "/x.mkv"},
        )
        self.assertEqual(status.triggered_by, TRIGGER_COMPLETION)

    def test_never_raises(self) -> None:
        """Even with totally garbage config the function returns a status,
        never raises — callers must be able to trust the return value."""
        status = send_webhook_notification(
            {"notification": None},  # type: ignore[typeddict-item]
            {"id": 13, "file_path": None},  # type: ignore[typeddict-item]
        )
        self.assertIn(status.state, {WEBHOOK_STATE_SKIPPED, WEBHOOK_STATE_FAILED, WEBHOOK_STATE_SUCCESS})


class MigrateNotificationConfigTests(unittest.TestCase):
    def test_backfills_missing_fields(self) -> None:
        cfg = {"webhook_enabled": True, "webhook_url": "http://x"}
        _migrate_notification_config(cfg)
        self.assertTrue(cfg["trigger_on_subtitle_change"])
        self.assertEqual(cfg["subtitle_change_debounce_seconds"], 5)

    def test_preserves_explicit_values(self) -> None:
        cfg = {
            "webhook_enabled": True,
            "trigger_on_subtitle_change": False,
            "subtitle_change_debounce_seconds": 30,
        }
        _migrate_notification_config(cfg)
        self.assertFalse(cfg["trigger_on_subtitle_change"])
        self.assertEqual(cfg["subtitle_change_debounce_seconds"], 30)

    def test_clamps_invalid_debounce(self) -> None:
        cfg = {"subtitle_change_debounce_seconds": -5}
        _migrate_notification_config(cfg)
        self.assertEqual(cfg["subtitle_change_debounce_seconds"], 5)

        cfg = {"subtitle_change_debounce_seconds": "oops"}
        _migrate_notification_config(cfg)
        self.assertEqual(cfg["subtitle_change_debounce_seconds"], 5)

    def test_no_op_on_empty(self) -> None:
        cfg: dict = {}
        _migrate_notification_config(cfg)
        self.assertTrue(cfg["trigger_on_subtitle_change"])
        self.assertEqual(cfg["subtitle_change_debounce_seconds"], 5)


if __name__ == "__main__":
    unittest.main()
