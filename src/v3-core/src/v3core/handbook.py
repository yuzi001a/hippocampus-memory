"""HandbookManager — simple CRUD handbook/recipe-book data store.

Stores entries as a JSON file at {basePath}/handbook/handbook.json.
Each entry has: title, content, tags, updated_at.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("v3core.handbook")


def _resolve_handbook_dir(config=None) -> Path:
    """Resolve the handbook directory from config or default.

    Priority:
      1. config.basePath / config.base_path — already normalized by resolve_config()
      2. fallback to ~/.v3-core/profiles/default/

    Accepts V3Config / dict / None.
    """
    base_path = None
    if config is not None:
        if isinstance(config, dict):
            base_path = config.get("basePath", "") or None
        else:
            base_path = getattr(config, "base_path", "") or None

    if base_path:
        return Path(base_path) / "handbook"
    return Path.home() / ".v3-core" / "profiles" / "default" / "handbook"


class HandbookManager:
    """Simple CRUD handbook data store backed by a single JSON file.

    File is read on every operation — no stale in-memory cache.
    """

    def __init__(self, config: dict | None = None):
        self._handbook_dir = _resolve_handbook_dir(config)
        self._data_path = self._handbook_dir / "handbook.json"

    # -- internal I/O --

    def _load(self) -> dict[str, Any]:
        """Load all entries from the JSON file. Returns {} if missing / empty."""
        if not self._data_path.exists():
            return {}
        try:
            raw = self._data_path.read_text(encoding="utf-8")
            if not raw or not raw.strip():
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load handbook: %s", e)
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        """Atomically write the full data dict to JSON (tmp + replace)."""
        self._handbook_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._data_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._data_path)

    # -- public API --

    def set(self, key: str, title: str, content: str,
            tags: list[str] | None = None) -> dict:
        """Write/overwrite an entry. NOT append — replaces existing key.

        Returns {"success": True, "key": key}.
        """
        data = self._load()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        data[key] = {
            "title": title,
            "content": content,
            "tags": tags or [],
            "updated_at": now,
        }
        self._save(data)
        return {"success": True, "key": key}

    def get(self, key: str) -> dict | None:
        """Return the full entry dict, or None if key does not exist."""
        data = self._load()
        return data.get(key)

    def list_all(self) -> dict[str, dict]:
        """Return all entries as {key: {title, updated_at}}."""
        data = self._load()
        return {
            k: {
                "title": v.get("title", ""),
                "updated_at": v.get("updated_at", ""),
            }
            for k, v in data.items()
        }

    def delete(self, key: str) -> bool:
        """Remove an entry. Returns True if the key existed, False otherwise."""
        data = self._load()
        if key not in data:
            return False
        del data[key]
        self._save(data)
        return True

    def get_index(self) -> dict[str, dict]:
        """Return index format compatible with recall_pool keyword engine.

        Returns {key: {title, tags, content_preview: content[:200]}}.
        """
        data = self._load()
        return {
            k: {
                "title": v.get("title", ""),
                "tags": v.get("tags", []),
                "content_preview": (v.get("content", "") or "")[:500],
            }
            for k, v in data.items()
        }
