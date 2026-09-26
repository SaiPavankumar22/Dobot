"""Memory service: the only sanctioned way to write or read durable memories."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.memory.embeddings import Embedder, build_embedder
from app.memory.retriever import Retriever
from app.memory.store import RecordStore
from app.memory.vectors import VectorStore, build_vector_store
from app.schemas import MemoryHit, MemoryRecord, MemoryType

logger = get_logger(__name__)


class MemoryManager:
    def __init__(
        self,
        *,
        store: RecordStore,
        vectors: VectorStore,
        embedder: Embedder,
        bus: EventBus | None = None,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.embedder = embedder
        self.bus = bus or get_event_bus()
        self.retriever = Retriever(vectors=vectors, embedder=embedder, store=store)

    async def remember(
        self,
        content: str,
        *,
        type: MemoryType = MemoryType.FACT,
        importance: float = 0.6,
        tags: Sequence[str] | None = None,
        source: str = "chat",
    ) -> MemoryRecord:
        record = MemoryRecord(
            type=type,
            content=content.strip(),
            importance=max(0.0, min(1.0, importance)),
            tags=list(tags or []),
            source=source,
        )
        await self.store.insert("memories", record.model_dump(mode="json"))
        try:
            vector = (await self.embedder.embed([record.content]))[0]
            await self.vectors.upsert(
                record.id,
                vector,
                {
                    "type": record.type.value,
                    "source": source,
                    "tags": record.tags,
                    "created_at": record.created_at.isoformat(),
                },
            )
            record.embedding_id = record.id
            await self.store.update("memories", record.id, {"embedding_id": record.id})
        except Exception as exc:  # noqa: BLE001 - memory must still be stored textually
            logger.warning("could not index memory %s (%s)", record.id, exc)

        await self.bus.emit(
            EventType.MEMORY_WRITTEN,
            message=f"Remembered ({record.type.value}): {record.content[:80]}",
            memory_id=record.id,
            memory_type=record.type.value,
        )
        return record

    async def recall(
        self,
        query: str,
        *,
        limit: int = 6,
        task_tags: Sequence[str] | None = None,
        types: Sequence[MemoryType] | None = None,
    ) -> list[MemoryHit]:
        hits = await self.retriever.search(
            query,
            limit=limit,
            task_tags=list(task_tags or []),
            types=list(types) if types else None,
        )
        # Recall is a *use*, and use is what keeps a memory alive in the decay model. Touched in one
        # batched write rather than one write per hit, because the local store flushes per operation.
        await self.touch([hit.memory.id for hit in hits])
        return hits

    async def touch(self, memory_ids: Sequence[str]) -> int:
        """Record that these memories were just recalled. Never raises: recall must not fail."""
        ids = [str(memory_id) for memory_id in memory_ids if memory_id]
        if not ids:
            return 0
        now = datetime.now(UTC).isoformat()
        patches: dict[str, dict[str, object]] = {}
        for memory_id in ids:
            try:
                doc = await self.store.get("memories", memory_id)
            except Exception:  # noqa: BLE001
                continue
            if not doc:
                continue
            patches[memory_id] = {
                "last_access_at": now,
                "access_count": int(doc.get("access_count", 0) or 0) + 1,
            }
        if not patches:
            return 0
        try:
            return await self.store.update_many("memories", patches)
        except Exception as exc:  # noqa: BLE001
            logger.info("could not record memory access (%s)", exc)
            return 0

    async def get(self, memory_id: str) -> MemoryRecord | None:
        doc = await self.store.get("memories", memory_id)
        return MemoryRecord.model_validate(doc) if doc else None

    async def list(
        self,
        *,
        type: MemoryType | None = None,
        limit: int = 200,
        search: str = "",
    ) -> list[MemoryRecord]:
        docs = await self.store.all("memories", limit=limit)
        records: list[MemoryRecord] = []
        for doc in docs:
            try:
                record = MemoryRecord.model_validate(doc)
            except Exception:  # noqa: BLE001
                continue
            if type and record.type != type:
                continue
            if search and search.lower() not in record.content.lower():
                continue
            records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records[:limit]

    async def forget(self, memory_id: str) -> bool:
        deleted = await self.store.delete("memories", memory_id)
        try:
            await self.vectors.delete(memory_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not delete vector for %s (%s)", memory_id, exc)
        return deleted

    async def prune(self, *, type: MemoryType | None = None, older_than_days: int = 30) -> int:
        cutoff = datetime.now(UTC).timestamp() - older_than_days * 86_400

        def predicate(doc: dict) -> bool:
            if type and doc.get("type") != type.value:
                return False
            created_at = doc.get("created_at")
            if not created_at:
                return False
            try:
                stamp = datetime.fromisoformat(str(created_at)).timestamp()
            except ValueError:
                return False
            return stamp < cutoff

        # Collect the doomed ids first so their vectors can be dropped by id. Rebuilding the whole
        # index for every prune meant re-embedding every surviving memory — O(n) model calls for one
        # deletion — and a surviving vector is correct by construction: ids mirror memory ids.
        doomed_ids = [
            str(doc.get("id"))
            for doc in await self.store.all("memories", limit=10_000)
            if predicate(doc) and doc.get("id")
        ]
        doomed = await self.store.delete_many("memories", predicate)
        for memory_id in doomed_ids:
            try:
                await self.vectors.delete(memory_id)
            except Exception:  # noqa: BLE001 - a stale vector is harmless, a failed prune is not
                continue
        return doomed

    async def stats(self) -> dict[str, object]:
        records = await self.list(limit=10_000)
        by_type: dict[str, int] = {}
        for record in records:
            by_type[record.type.value] = by_type.get(record.type.value, 0) + 1
        recalled = sum(record.access_count for record in records)
        return {
            "total": len(records),
            "by_type": by_type,
            "store": self.store.name,
            "vectors": self.vectors.name,
            "embedder": self.embedder.name,
            "never_recalled": sum(1 for record in records if record.access_count == 0),
            "recall_events": recalled,
        }

    async def close(self) -> None:
        await self.vectors.close()


def build_memory_manager(store: RecordStore, bus: EventBus | None = None) -> MemoryManager:
    embedder = build_embedder()
    vectors = build_vector_store(dim=getattr(embedder, "dim", 512))
    return MemoryManager(store=store, vectors=vectors, embedder=embedder, bus=bus)
