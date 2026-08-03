"""Media server webhook notifications (Jellyfin/Emby/Plex)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# Trigger constants — identifies why a webhook is being sent.
# Callers pass these to send_webhook_notification(); the value is also
# surfaced in WebhookStatus so the UI / API can distinguish paths.
TRIGGER_COMPLETION = "completion"
TRIGGER_SUBTITLE_CHANGE = "subtitle_change"

# State constants on WebhookStatus.state
WEBHOOK_STATE_SUCCESS = "success"
WEBHOOK_STATE_FAILED = "failed"
WEBHOOK_STATE_SKIPPED = "skipped"


@dataclass
class WebhookStatus:
    """Outcome of a webhook send attempt.

    Attributes:
        state: One of WEBHOOK_STATE_* ("success" / "failed" / "skipped").
        webhook_type: "jellyfin" | "emby" | "plex" | "generic" | None.
        triggered_by: One of TRIGGER_* ("completion" | "subtitle_change").
        error: Error message if state == "failed", else None.
        detail: Human-readable reason for "skipped" (e.g. "webhook_enabled is false",
            "webhook_url not set", "trigger_on_subtitle_change disabled").
    """

    state: str
    webhook_type: str | None
    triggered_by: str
    error: str | None = None
    detail: str | None = None


def send_webhook_notification(
    config: dict[str, Any],
    task: dict[str, Any],
    trigger: str = TRIGGER_COMPLETION,
) -> WebhookStatus:
    """Send a library-scan webhook to the configured media server.

    Args:
        config: Full app config (must contain `notification` block).
        task: Task dict (must contain `id` and `file_path`).
        trigger: Why the webhook is being sent. Defaults to TRIGGER_COMPLETION
            (preserves backward compatibility for callers that don't pass this).
            Pass TRIGGER_SUBTITLE_CHANGE when called from subtitle edit flows.

    Returns:
        WebhookStatus describing the outcome. Never raises — all exceptions
        are caught and reported as state="failed".
    """
    notif_cfg = config.get("notification") or {}
    if not notif_cfg.get("webhook_enabled"):
        return WebhookStatus(
            state=WEBHOOK_STATE_SKIPPED,
            webhook_type=None,
            triggered_by=trigger,
            detail="webhook_enabled is false",
        )

    webhook_url = str(notif_cfg.get("webhook_url", "")).strip()
    if not webhook_url:
        return WebhookStatus(
            state=WEBHOOK_STATE_SKIPPED,
            webhook_type=None,
            triggered_by=trigger,
            detail="webhook_url not set",
        )

    webhook_type = str(notif_cfg.get("webhook_type", "jellyfin")).strip().lower()
    token = str(notif_cfg.get("webhook_token", "")).strip()
    library_id_raw = str(notif_cfg.get("webhook_library_id", "")).strip()

    file_path = str(task.get("file_path", ""))
    dir_path = str(Path(file_path).parent) if file_path else ""

    try:
        if webhook_type == "jellyfin":
            _notify_jellyfin(webhook_url, token, library_id_raw, dir_path)
        elif webhook_type == "emby":
            _notify_emby(webhook_url, token, library_id_raw, dir_path)
        elif webhook_type == "plex":
            _notify_plex(webhook_url, token, library_id_raw, dir_path)
        else:
            _notify_generic(webhook_url, token, task)
        logger.info(
            "Webhook (%s) sent for task %s (trigger=%s)",
            webhook_type, task.get("id"), trigger,
        )
        return WebhookStatus(
            state=WEBHOOK_STATE_SUCCESS,
            webhook_type=webhook_type,
            triggered_by=trigger,
        )
    except Exception as exc:
        logger.warning(
            "Webhook (%s) failed for task %s (trigger=%s): %s",
            webhook_type, task.get("id"), trigger, exc,
        )
        return WebhookStatus(
            state=WEBHOOK_STATE_FAILED,
            webhook_type=webhook_type,
            triggered_by=trigger,
            error=str(exc),
        )


def _notify_jellyfin(base_url: str, token: str, library_ids: str, dir_path: str) -> None:
    """Trigger Jellyfin library scan via API."""
    url = urljoin(base_url.rstrip("/") + "/", "Library/Media/Updated")
    headers = {"Authorization": f'MediaBrowser Token="{token}"'} if token else {}
    libraries = [s.strip() for s in library_ids.split(",") if s.strip()] if library_ids else []
    payload: dict[str, Any] = {"Updates": []}
    if libraries:
        payload["Updates"] = [{"Path": dir_path, "Mode": "Update"}] if dir_path else []
    else:
        # No library ID configured — scan all
        url = urljoin(base_url.rstrip("/") + "/", "Library/Refresh")
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


def _notify_emby(base_url: str, token: str, library_ids: str, dir_path: str) -> None:
    """Trigger Emby library scan via API."""
    url = urljoin(base_url.rstrip("/") + "/", "Library/Media/Updated")
    headers = {"X-Emby-Token": token} if token else {}
    libraries = [s.strip() for s in library_ids.split(",") if s.strip()] if library_ids else []
    payload: dict[str, Any] = {"Updates": []}
    if libraries:
        payload["Updates"] = [{"Path": dir_path, "Mode": "Update"}] if dir_path else []
    else:
        url = urljoin(base_url.rstrip("/") + "/", "Library/Refresh")
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()


def _notify_plex(base_url: str, token: str, section_ids: str, dir_path: str) -> None:
    """Trigger Plex library scan via API.

    Note: For Plex, the "token" parameter carries the X-Plex-Token (Plex auth
    is via the X-Plex-Token header, not a separate API token), and the
    "section_ids" parameter carries the comma-separated Plex library section IDs.
    """
    headers = {"X-Plex-Token": token} if token else {}
    sections = [s.strip() for s in section_ids.split(",") if s.strip()] if section_ids else []
    with httpx.Client(timeout=15) as client:
        if sections:
            for section_id in sections:
                url = urljoin(
                    base_url.rstrip("/") + "/",
                    f"library/sections/{section_id}/refresh",
                )
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
        else:
            url = urljoin(base_url.rstrip("/") + "/", "library/sections/all/refresh")
            resp = client.get(url, headers=headers)
            resp.raise_for_status()


def _notify_generic(url: str, token: str, task: dict[str, Any]) -> None:
    """Send a generic POST webhook with task info."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {
        "event": "subtitle.completed",
        "task_id": task.get("id"),
        "file_path": task.get("file_path"),
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
