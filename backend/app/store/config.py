"""Configuration CRUD + in-process cache, extracted from `Database`.

Owns:
- `get_config()` — read-through cache keyed by `system.config_version`
- `update_config()` — write + cache invalidation + version bump
- `_get_config_uncached()` — raw DB read merged onto defaults
- `_get_config_version()` / `_bump_config_version()` — cross-process cache coherence
- Whisper config normalization (`align_method` → `align_provider`)

Like `DatabaseMigrations`, this class holds a back-reference to its parent
`Database` for connection management. The in-process cache state lives on
this class so the schema layer can be redeployed without touching cache
invariants.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from ..defaults import SYSTEM_LEVEL_FIELDS, copy_default_config, detect_device

if TYPE_CHECKING:
    from . import Database


# Mutating these fields is refused by the API. `whisper.device` is the only
# read-only field today (auto-detected at runtime). Re-exported from
# ``store.__init__`` for tests and api handlers.
READ_ONLY_CONFIG_FIELDS: set[tuple[str, str]] = {
    ("whisper", "device"),
}


def _normalize_legacy_align_method(value: Any) -> str:
    """Map legacy `whisper.align_method` values onto the new `align_provider` enum."""
    normalized = str(value or "auto").strip().lower()
    legacy_mapping = {
        "whisperx": "auto",
        "auto": "auto",
        "simple": "none",
        "none": "none",
    }
    return legacy_mapping.get(normalized, normalized or "auto")


def _normalize_align_provider(value: Any) -> str:
    """Clamp `align_provider` to the supported set; fall back to 'auto'."""
    normalized = str(value or "auto").strip().lower()
    supported = {"auto", "whisperx", "qwen-forced", "none"}
    return normalized if normalized in supported else "auto"


def _migrate_whisper_config_dict(whisper_config: dict[str, Any]) -> None:
    """Normalize legacy `align_method` → `align_provider` in place.

    Idempotent. Kept at module scope so external callers (and the legacy
    `Database._migrate_whisper_config_dict` accessor) can import it without
    instantiating `ConfigService`.
    """
    if "align_provider" in whisper_config:
        whisper_config["align_provider"] = _normalize_align_provider(whisper_config.get("align_provider"))
    elif "align_method" in whisper_config:
        whisper_config["align_provider"] = _normalize_legacy_align_method(whisper_config.get("align_method"))
    whisper_config.pop("align_method", None)


class ConfigService:
    """Read/write the `system_config` table with an in-process cache.

    Cache coherence across processes is provided by a monotonically increasing
    `system.config_version` integer row. `get_config()` reads the version on
    every call; on mismatch the cached dict is discarded and rebuilt from DB.
    `update_config()` always bumps the version inside the same transaction as
    the write, then invalidates the in-process cache.
    """

    def __init__(self, database: "Database") -> None:
        self.database = database
        # In-process cache state. Lives here, not on Database — Database
        # exposes property proxies for backward compatibility with tests that
        # mutate `database._cached_config_version` directly.
        self._config_cache: dict[str, Any] | None = None
        self._cache_valid: bool = False
        self._cached_config_version: int | None = None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        """Return the merged config dict, populating the cache if necessary.

        On every call we re-read `system.config_version` from the DB; if it
        matches the version used to build the current cache, the cache is
        returned without touching `system_config` otherwise.
        """
        current_version = self._get_config_version()
        if (
            self._cache_valid
            and self._config_cache is not None
            and self._cached_config_version == current_version
        ):
            return self._config_cache
        self._config_cache = self._get_config_uncached()
        self._cache_valid = True
        self._cached_config_version = current_version
        return self._config_cache

    def _get_config_uncached(self) -> dict[str, Any]:
        """Read every `system_config` row and merge onto defaults.

        - `system.*` rows are filtered out of the user-visible config.
        - Legacy `whisper.align_method` is normalized to `align_provider`.
        - `whisper.device == 'auto'` is resolved to the runtime device.
        - A synthetic `meta.restart_required` flag is attached.
        """
        defaults = copy_default_config()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT group_name, key_name, value_json, scope, restart_required, updated_at
                FROM system_config
                ORDER BY group_name, key_name
                """
            ).fetchall()
        restart_required = False
        for row in rows:
            restart_required = restart_required or bool(row["restart_required"] and row["scope"] == "system")
            if row["group_name"] == "system":
                continue
            defaults.setdefault(row["group_name"], {})[row["key_name"]] = json.loads(row["value_json"])
        whisper_config = defaults.setdefault("whisper", {})
        _migrate_whisper_config_dict(whisper_config)
        if defaults.get("whisper", {}).get("device") == "auto":
            defaults["whisper"]["device"] = detect_device()
        defaults["meta"] = {"restart_required": restart_required}
        return defaults

    def _get_config_version(self) -> int:
        """Read `system.config_version`. Returns 0 if the row is missing or malformed.

        Used by `get_config()` for cross-process cache invalidation: another
        process bumping the version makes our cache stale on the next read.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT value_json
                FROM system_config
                WHERE group_name = 'system' AND key_name = 'config_version'
                """
            ).fetchone()
        if row is None:
            return 0
        try:
            return int(json.loads(row["value_json"]))
        except (ValueError, TypeError):
            return 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial config update, bump version, invalidate cache.

        - Rejects unknown fields with `KeyError`.
        - Silently skips read-only fields (e.g. `whisper.device`).
        - Normalizes legacy `whisper.align_method` → `align_provider`.
        - When any `system`-scoped key changes, sets `restart_required=1` on
          every `system` row so the frontend can prompt the user.
        """
        from . import utc_now

        updated_system_key = False
        with self.database.connect() as connection:
            current = self._get_config_uncached()
            now = utc_now()
            for group_name, group_values in payload.items():
                if not isinstance(group_values, dict):
                    continue
                for key_name, value in group_values.items():
                    if group_name == "whisper" and key_name == "align_method":
                        key_name = "align_provider"
                        value = _normalize_legacy_align_method(value)
                    elif group_name == "whisper" and key_name == "align_provider":
                        value = _normalize_align_provider(value)
                    if (group_name, key_name) in READ_ONLY_CONFIG_FIELDS:
                        continue
                    if group_name not in current or key_name not in current[group_name]:
                        raise KeyError(f"unknown config field: {group_name}.{key_name}")
                    scope = "system" if (group_name, key_name) in SYSTEM_LEVEL_FIELDS else "runtime"
                    restart_required = 1 if scope == "system" else 0
                    if scope == "system" and current[group_name][key_name] != value:
                        updated_system_key = True
                    connection.execute(
                        """
                        INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(group_name, key_name)
                        DO UPDATE SET value_json = excluded.value_json,
                                      scope = excluded.scope,
                                      restart_required = excluded.restart_required,
                                      updated_at = excluded.updated_at
                        """,
                        (group_name, key_name, json.dumps(value), scope, restart_required, now),
                    )
            if updated_system_key:
                connection.execute(
                    """
                    UPDATE system_config
                    SET restart_required = CASE WHEN scope = 'system' THEN 1 ELSE restart_required END,
                        updated_at = ?
                    """,
                    (now,),
                )
            self._bump_config_version(connection)
        self._cache_valid = False
        return self.get_config()

    def _bump_config_version(self, connection: sqlite3.Connection) -> int:
        """Atomically increment `system.config_version` and return the new value.

        The row is first ensured via `INSERT ... ON CONFLICT DO NOTHING`, then
        incremented in-place. Reads back the post-increment integer so callers
        can use it without a second round-trip.
        """
        from . import utc_now

        now = utc_now()
        connection.execute(
            """
            INSERT INTO system_config (group_name, key_name, value_json, scope, restart_required, updated_at)
            VALUES ('system', 'config_version', '0', 'system', 0, ?)
            ON CONFLICT(group_name, key_name) DO NOTHING
            """,
            (now,),
        )
        connection.execute(
            """
            UPDATE system_config
            SET value_json = CAST(CAST(value_json AS INTEGER) + 1 AS TEXT),
                updated_at = ?
            WHERE group_name = 'system' AND key_name = 'config_version'
            """,
            (now,),
        )
        row = connection.execute(
            """
            SELECT value_json
            FROM system_config
            WHERE group_name = 'system' AND key_name = 'config_version'
            """
        ).fetchone()
        try:
            return int(json.loads(row["value_json"])) if row else 0
        except (ValueError, TypeError):
            return 0
