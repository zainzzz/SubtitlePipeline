"""Media server webhook notifications (Jellyfin/Emby/Plex)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


def send_webhook_notification(config: dict[str, Any], task: dict[str, Any]) -> None:
    """Send a library-scan webhook to the configured media server after a task completes."""
    notif_cfg = config.get("notification", {})
    if not notif_cfg.get("webhook_enabled"):
        return

    webhook_url = str(notif_cfg.get("webhook_url", "")).strip()
    if not webhook_url:
        logger.debug("Webhook enabled but no URL set, skipping")
        return

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
        logger.info("Webhook (%s) sent for task %s", webhook_type, task.get("id"))
    except Exception as exc:
        logger.warning("Webhook (%s) failed: %s", webhook_type, exc)


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
    """Trigger Plex library scan via API."""
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
