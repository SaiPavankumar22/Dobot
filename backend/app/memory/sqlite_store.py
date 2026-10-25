"""SQLite-backed local record store.

One database replaces the JSON-per-collection files for local desktop installs. The public
interface intentionally matches ``RecordStore`` so the orchestrator, memory manager, scheduler,
approvals, and traces do not need to know which local database is active.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiosqlite

from app.logging_setup import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1


class SQLiteRecordStore:
    name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            self._connection = await aiosqlite.connect(str(self.path))
            self._connection.row_factory = aiosqlite.Row
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA foreign_keys=ON")
            await self._connection.execute("PRAGMA busy_timeout=5000")
            await self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    user_id TEXT,
                    conversation_id TEXT,
                    PRIMARY KEY (collection, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_collection_updated
                    ON records(collection, updated_at);
                CREATE INDEX IF NOT EXISTS idx_records_user
                    ON records(collection, user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_records_conversation
                    ON records(collection, conversation_id, updated_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
                    collection UNINDEXED,
                    record_id UNINDEXED,
                    text
                );
                """
            )
            row = await (await self._connection.execute("SELECT version FROM schema_version LIMIT 1")).fetchone()
            if row is None:
                await self._connection.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif int(row["version"]) < SCHEMA_VERSION:
                await self._connection.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )
            await self._connection.commit()
        return self._connection

    @staticmethod
    def _metadata(doc: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
        return (
            str(doc.get("created_at")) if doc.get("created_at") is not None else None,
            str(doc.get("updated_at")) if doc.get("updated_at") is not None else None,
            str(doc.get("user_id")) if doc.get("user_id") is not None else None,
            str(doc.get("conversation_id")) if doc.get("conversation_id") is not None else None,
        )

    @staticmethod
    def _search_text(doc: dict[str, Any]) -> str:
        fields = ("title", "description", "content", "answer", "message", "name", "detail")
        return " ".join(str(doc.get(field, "")) for field in fields if doc.get(field))

    async def insert(self, collection: str, doc: dict[str, Any]) -> dict[str, Any]:
        record_id = str(doc.get("id") or doc.get("_id") or "")
        if not record_id:
            raise ValueError("SQLite records require an id")
        async with self._lock:
            db = await self._db()
            created_at, updated_at, user_id, conversation_id = self._metadata(doc)
            await db.execute(
                """
                INSERT INTO records(collection, record_id, payload, created_at, updated_at, user_id, conversation_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(collection, record_id) DO UPDATE SET
                    payload=excluded.payload, created_at=excluded.created_at, updated_at=excluded.updated_at,
                    user_id=excluded.user_id, conversation_id=excluded.conversation_id
                """,
                (
                    collection,
                    record_id,
                    json.dumps(doc, ensure_ascii=False, default=str),
                    created_at,
                    updated_at,
                    user_id,
                    conversation_id,
                ),
            )
            await db.execute(
                "DELETE FROM records_fts WHERE collection = ? AND record_id = ?",
                (collection, record_id),
            )
            await db.execute(
                "INSERT INTO records_fts(collection, record_id, text) VALUES (?, ?, ?)",
                (collection, record_id, self._search_text(doc)),
            )
            await db.commit()
        return dict(doc)

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        async with self._lock:
            db = await self._db()
            cursor = await db.execute(
                "SELECT payload FROM records WHERE collection = ? AND record_id = ?",
                (collection, doc_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return json.loads(row["payload"]) if row else None

    async def all(self, collection: str, limit: int = 500) -> list[dict[str, Any]]:
        async with self._lock:
            db = await self._db()
            cursor = await db.execute(
                """
                SELECT payload FROM records
                WHERE collection = ?
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (collection, max(1, limit)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [json.loads(row["payload"]) for row in reversed(rows)]

    async def update(
        self, collection: str, doc_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        current = await self.get(collection, doc_id)
        if current is None:
            return None
        current.update(patch)
        await self.insert(collection, current)
        return current

    async def update_many(self, collection: str, patches: dict[str, dict[str, Any]]) -> int:
        changed = 0
        for doc_id, patch in patches.items():
            if await self.update(collection, doc_id, patch) is not None:
                changed += 1
        return changed

    async def delete(self, collection: str, doc_id: str) -> bool:
        async with self._lock:
            db = await self._db()
            cursor = await db.execute(
                "DELETE FROM records WHERE collection = ? AND record_id = ?",
                (collection, doc_id),
            )
            await db.execute(
                "DELETE FROM records_fts WHERE collection = ? AND record_id = ?",
                (collection, doc_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_many(self, collection: str, predicate: Callable[[dict[str, Any]], bool]) -> int:
        doomed = [
            str(doc.get("id"))
            for doc in await self.all(collection, limit=100_000)
            if doc.get("id") and predicate(doc)
        ]
        removed = 0
        for doc_id in doomed:
            removed += int(await self.delete(collection, doc_id))
        return removed

    async def count(self, collection: str) -> int:
        async with self._lock:
            db = await self._db()
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM records WHERE collection = ?", (collection,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row["count"])

    async def search(self, query: str, *, collection: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        match = query.strip().replace('"', '""')
        if not match:
            return []
        async with self._lock:
            db = await self._db()
            sql = """
                SELECT r.payload FROM records_fts f
                JOIN records r ON r.collection = f.collection AND r.record_id = f.record_id
                WHERE records_fts MATCH ?
            """
            args: list[Any] = [f'"{match}"*']
            if collection:
                sql += " AND f.collection = ?"
                args.append(collection)
            sql += " ORDER BY rank LIMIT ?"
            args.append(max(1, limit))
            cursor = await db.execute(sql, args)
            rows = await cursor.fetchall()
            await cursor.close()
        return [json.loads(row["payload"]) for row in rows]

    async def health(self) -> dict[str, Any]:
        db = await self._db()
        cursor = await db.execute("PRAGMA integrity_check")
        row = await cursor.fetchone()
        await cursor.close()
        version_cursor = await db.execute("SELECT version FROM schema_version LIMIT 1")
        version_row = await version_cursor.fetchone()
        await version_cursor.close()
        return {
            "engine": self.name,
            "path": str(self.path),
            "exists": self.path.exists(),
            "schema_version": int(version_row["version"]) if version_row else 0,
            "integrity": str(row[0]) if row else "unknown",
            "wal": True,
            "bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    async def checkpoint(self) -> dict[str, Any]:
        async with self._lock:
            db = await self._db()
            cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = await cursor.fetchone()
            await cursor.close()
            await db.commit()
        return {"busy": int(row[0]) if row else 0, "log_pages": int(row[1]) if row else 0, "checkpointed_pages": int(row[2]) if row else 0}

    async def vacuum(self) -> None:
        async with self._lock:
            db = await self._db()
            await db.execute("VACUUM")
            await db.commit()

    async def backup(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            db = await self._db()
            await db.commit()
            await db.close()
            self._connection = None
            shutil.copy2(self.path, destination)
        return destination

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.commit()
                await self._connection.close()
                self._connection = None
