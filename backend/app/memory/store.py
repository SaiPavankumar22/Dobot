"""Local structured record storage.

SQLite is the only durable record store. It runs entirely on the user's machine, needs no server,
and is migrated from the legacy JSON collection files on first startup.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

COLLECTIONS = (
    "tasks",
    "memories",
    "automations",
    "approvals",
    "activity",
    "screen",
    "settings",
    "canonical",
    "runs",
    "traces",
    "grants",
    "users",
    "invites",
)


class RecordStore(Protocol):
    name: str

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]: ...
    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]: ...
    async def update(
        self, collection: str, doc_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None: ...
    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int: ...
    async def delete(self, collection: str, doc_id: str) -> bool: ...
    async def delete_many(
        self, collection: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> int: ...
    async def count(self, collection: str) -> int: ...
    async def close(self) -> None: ...


def _matches(doc: dict[str, Any], predicate: Callable[[dict[str, Any]], bool]) -> bool:
    try:
        return bool(predicate(doc))
    except Exception:  # noqa: BLE001 - a bad predicate must not break a query
        return False


class LocalFileStore:
    """Legacy JSON-per-collection store retained only for migration and compatibility."""

    name = "local-file-store"

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _path(self, collection: str) -> Path:
        return self._dir / f"{collection}.json"

    def _load(self, collection: str) -> dict[str, dict[str, Any]]:
        if collection in self._cache:
            return self._cache[collection]
        path = self._path(collection)
        data: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(key): value for key, value in raw.items()}
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("could not read %s (%s); starting empty", path.name, exc)
        self._cache[collection] = data
        return data

    def _flush(self, collection: str) -> None:
        data = self._cache.get(collection, {})
        path = self._path(collection)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            data = self._load(collection)
            doc_id = str(doc.get("id") or doc.get("_id"))
            data[doc_id] = dict(doc)
            self._flush(collection)
            return data[doc_id]

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        async with self._lock:
            doc = self._load(collection).get(doc_id)
            return dict(doc) if doc else None

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(doc) for doc in list(self._load(collection).values())[-limit:]]

    async def update(
        self, collection: str, doc_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        async with self._lock:
            data = self._load(collection)
            if doc_id not in data:
                return None
            data[doc_id].update(patch)
            self._flush(collection)
            return dict(data[doc_id])

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int:
        if not patches:
            return 0
        async with self._lock:
            data = self._load(collection)
            changed = 0
            for doc_id, patch in patches.items():
                if doc_id in data:
                    data[doc_id].update(patch)
                    changed += 1
            if changed:
                self._flush(collection)
            return changed

    async def delete(self, collection: str, doc_id: str) -> bool:
        async with self._lock:
            data = self._load(collection)
            if doc_id in data:
                del data[doc_id]
                self._flush(collection)
                return True
            return False

    async def delete_many(
        self, collection: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> int:
        async with self._lock:
            data = self._load(collection)
            doomed = [key for key, doc in data.items() if _matches(doc, predicate)]
            for key in doomed:
                del data[key]
            if doomed:
                self._flush(collection)
            return len(doomed)

    async def count(self, collection: str) -> int:
        async with self._lock:
            return len(self._load(collection))

    async def close(self) -> None:
        async with self._lock:
            for collection in list(self._cache):
                self._flush(collection)


async def build_record_store(settings: Settings | None = None) -> RecordStore:
    settings = settings or get_settings()
    from app.memory.sqlite_store import SQLiteRecordStore
    from app.memory.storage_migration import migrate_json_store

    local = SQLiteRecordStore(settings.data_dir / "dobot.sqlite3")
    migration = await migrate_json_store(
        settings.state_dir / "store",
        local,
        backup_directory=settings.data_dir / "backups" / "json-store",
    )
    if migration.get("migrated") or migration.get("backup"):
        logger.info("local JSON store migration: %s", migration)
    logger.info("record store: local SQLite (%s)", settings.data_dir / "dobot.sqlite3")
    return local


def build_iter(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(values)
