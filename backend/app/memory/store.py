"""Structured record storage.

Live path: MongoDB. Fallback: a file-backed store under ``DOBOT_STATE_DIR`` that keeps working
offline and survives restarts. Both implement the same small interface so nothing else in the
codebase knows which one is in use.
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
    "grants",
)


class RecordStore(Protocol):
    name: str

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]: ...

    async def update(self, collection: str, doc_id: str, patch: dict[str, Any]) -> dict[str, Any] | None: ...

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int: ...

    async def delete(self, collection: str, doc_id: str) -> bool: ...

    async def delete_many(self, collection: str, predicate: Callable[[dict[str, Any]], bool]) -> int: ...

    async def count(self, collection: str) -> int: ...

    async def close(self) -> None: ...


def _matches(doc: dict[str, Any], predicate: Callable[[dict[str, Any]], bool]) -> bool:
    try:
        return bool(predicate(doc))
    except Exception:  # noqa: BLE001 - a bad predicate must not break a query
        return False


class LocalFileStore:
    """JSON-per-collection store with atomic writes and an in-process cache."""

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
        """Apply many patches in one lock and one flush.

        Recall touches several memories at once; doing that as N single updates would rewrite the
        whole collection file N times per turn, which is why this exists.
        """
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


class MongoRecordStore:
    """MongoDB-backed store (documents keyed by Dobot's own string id)."""

    name = "mongodb"

    def __init__(self, uri: str, database: str) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore

        self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=2500)
        self._db = self._client[database]

    def _collection(self, name: str) -> Any:
        return self._db[name]

    async def ping(self) -> bool:
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("mongodb ping failed (%s)", exc)
            return False

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        payload = dict(doc)
        payload["_id"] = str(payload.get("id") or payload.get("_id"))
        await self._collection(collection).replace_one({"_id": payload["_id"]}, payload, upsert=True)
        return doc

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = await self._collection(collection).find_one({"_id": doc_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return doc

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]:
        cursor = self._collection(collection).find({}).sort("_id", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        for doc in docs:
            doc.pop("_id", None)
        return list(reversed(docs))

    async def update(
        self, collection: str, doc_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        result = await self._collection(collection).find_one_and_update(
            {"_id": doc_id}, {"$set": patch}, return_document=True
        )
        if not result:
            return None
        result.pop("_id", None)
        return result

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int:
        if not patches:
            return 0
        from pymongo import UpdateOne  # type: ignore

        operations = [
            UpdateOne({"_id": doc_id}, {"$set": patch}) for doc_id, patch in patches.items()
        ]
        result = await self._collection(collection).bulk_write(operations, ordered=False)
        return int(result.modified_count)

    async def delete(self, collection: str, doc_id: str) -> bool:
        result = await self._collection(collection).delete_one({"_id": doc_id})
        return bool(result.deleted_count)

    async def delete_many(
        self, collection: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> int:
        docs = await self.all(collection, limit=10_000)
        doomed = [str(doc.get("id")) for doc in docs if _matches(doc, predicate) and doc.get("id")]
        if not doomed:
            return 0
        result = await self._collection(collection).delete_many({"_id": {"$in": doomed}})
        return int(result.deleted_count)

    async def count(self, collection: str) -> int:
        return int(await self._collection(collection).count_documents({}))

    async def close(self) -> None:
        self._client.close()


class FallbackRecordStore:
    """Prefers MongoDB, transparently rotates to the local store if it is unavailable."""

    def __init__(self, primary: RecordStore, fallback: RecordStore) -> None:
        self._primary = primary
        self._fallback = fallback
        self.name = primary.name
        self._degraded = False

    async def _run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self._degraded:
            try:
                return await getattr(self._primary, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("record store %s failed (%s); using local store", self._primary.name, exc)
                self._degraded = True
                self.name = f"{self._primary.name}->{self._fallback.name}"
        return await getattr(self._fallback, method)(*args, **kwargs)

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._run("insert", collection, doc))

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        return await self._run("get", collection, doc_id)

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]:
        return list(await self._run("all", collection, limit))

    async def update(
        self, collection: str, doc_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        return await self._run("update", collection, doc_id, patch)

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int:
        return int(await self._run("update_many", collection, patches))

    async def delete(self, collection: str, doc_id: str) -> bool:
        return bool(await self._run("delete", collection, doc_id))

    async def delete_many(
        self, collection: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> int:
        return int(await self._run("delete_many", collection, predicate))

    async def count(self, collection: str) -> int:
        return int(await self._run("count", collection))

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()

    @property
    def degraded(self) -> bool:
        return self._degraded


async def migrate_local_into(primary: RecordStore, local: RecordStore) -> int:
    """Copy documents from the local store into a newly available primary store."""
    moved = 0
    for collection in COLLECTIONS:
        for doc in await local.all(collection, limit=10_000):
            if doc.get("id"):
                await primary.insert(collection, doc)
                moved += 1
    return moved


async def build_record_store(settings: Settings | None = None) -> RecordStore:
    settings = settings or get_settings()
    local = LocalFileStore(settings.state_dir / "store")
    if settings.has_mongo:
        try:
            mongo = MongoRecordStore(settings.mongodb_uri, settings.mongodb_db)
            if await mongo.ping():
                await migrate_local_into(mongo, local)
                return FallbackRecordStore(mongo, local)
            await mongo.close()
        except Exception as exc:  # noqa: BLE001 - missing motor, bad URI, auth failure
            logger.warning("mongodb unavailable (%s); using local file store", exc)
    return local


def build_iter(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(values)
