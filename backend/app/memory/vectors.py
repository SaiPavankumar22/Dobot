"""Vector index for semantic memory.

Live path: Zilliz (Milvus-compatible). Fallback: an in-process cosine index, which is plenty for a
single user's memory and keeps the system runnable with zero infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class VectorHit:
    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    name: str

    async def upsert(self, doc_id: str, vector: list[float], metadata: dict[str, Any]) -> None: ...

    async def search(
        self, vector: list[float], limit: int = 6, filters: dict[str, Any] | None = None
    ) -> list[VectorHit]: ...

    async def delete(self, doc_id: str) -> bool: ...

    async def count(self) -> int: ...

    async def close(self) -> None: ...


class LocalVectorStore:
    """Cosine similarity index held in memory."""

    name = "local-vectors"

    def __init__(self) -> None:
        self._vectors: dict[str, np.ndarray] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    async def upsert(self, doc_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        self._vectors[doc_id] = array / norm if norm else array
        self._metadata[doc_id] = dict(metadata)

    async def search(
        self, vector: list[float], limit: int = 6, filters: dict[str, Any] | None = None
    ) -> list[VectorHit]:
        if not self._vectors:
            return []
        query = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm:
            query = query / norm
        hits: list[VectorHit] = []
        for doc_id, stored in self._vectors.items():
            metadata = self._metadata.get(doc_id, {})
            if filters and any(metadata.get(key) != value for key, value in filters.items()):
                continue
            if stored.shape != query.shape:
                continue
            hits.append(VectorHit(id=doc_id, score=float(np.dot(stored, query)), metadata=metadata))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    async def delete(self, doc_id: str) -> bool:
        self._metadata.pop(doc_id, None)
        return self._vectors.pop(doc_id, None) is not None

    async def count(self) -> int:
        return len(self._vectors)

    async def close(self) -> None:
        self._vectors.clear()
        self._metadata.clear()


class ZillizVectorStore:
    """Zilliz Cloud / Milvus collection with a dynamic-dimension schema."""

    name = "zilliz"
    _FIELD = "embedding"

    def __init__(self, settings: Settings, dim: int) -> None:
        self._settings = settings
        self._dim = dim
        self._client: Any = None
        self._collection: Any = None

    async def _ensure(self) -> None:
        if self._collection is not None:
            return
        from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient  # type: ignore

        token = self._settings.zilliz_token or None
        self._client = MilvusClient(uri=self._settings.zilliz_uri, token=token)
        name = self._settings.zilliz_collection
        if not self._client.has_collection(name):
            fields = [
                FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=128),
                FieldSchema(name=self._FIELD, dtype=DataType.FLOAT_VECTOR, dim=self._dim),
                FieldSchema(name="metadata", dtype=DataType.JSON),
            ]
            schema = CollectionSchema(fields=fields, description="Dobot semantic memory")
            self._client.create_collection(collection_name=name, schema=schema)
            index_params = self._client.prepare_index_params()
            index_params.add_index(
                field_name=self._FIELD, index_type="AUTOINDEX", metric_type="COSINE"
            )
            self._client.create_index(collection_name=name, index_params=index_params)
        self._collection = name

    async def upsert(self, doc_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        await self._ensure()
        self._client.upsert(
            collection_name=self._collection,
            data=[{"id": doc_id, self._FIELD: vector, "metadata": metadata or {}}],
        )

    async def search(
        self, vector: list[float], limit: int = 6, filters: dict[str, Any] | None = None
    ) -> list[VectorHit]:
        await self._ensure()
        expression = ""
        if filters:
            clauses = [
                f'metadata["{key}"] == "{value}"'
                for key, value in filters.items()
                if isinstance(value, (str, int, float, bool))
            ]
            expression = " and ".join(clauses)
        results = self._client.search(
            collection_name=self._collection,
            data=[vector],
            limit=limit,
            output_fields=["metadata"],
            filter=expression or None,
        )
        hits: list[VectorHit] = []
        for row in (results[0] if results else []):
            hits.append(
                VectorHit(
                    id=str(row.get("id")),
                    score=float(row.get("distance", 0.0)),
                    metadata=dict(row.get("entity", {}).get("metadata") or {}),
                )
            )
        return hits

    async def delete(self, doc_id: str) -> bool:
        await self._ensure()
        self._client.delete(collection_name=self._collection, ids=[doc_id])
        return True

    async def count(self) -> int:
        await self._ensure()
        stats = self._client.get_collection_stats(collection_name=self._collection)
        return int(stats.get("row_count", 0))

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()


class FallbackVectorStore:
    """Tries Zilliz once; on failure keeps working against the local index."""

    def __init__(self, primary: VectorStore, fallback: VectorStore) -> None:
        self._primary = primary
        self._fallback = fallback
        self.name = primary.name
        self._degraded = False

    async def _use_primary(self) -> bool:
        return not self._degraded

    async def _run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if await self._use_primary():
            try:
                return await getattr(self._primary, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("vector store %s failed (%s); using local index", self._primary.name, exc)
                self._degraded = True
                self.name = f"{self._primary.name}->{self._fallback.name}"
        return await getattr(self._fallback, method)(*args, **kwargs)

    async def upsert(self, doc_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        await self._run("upsert", doc_id, vector, metadata)

    async def search(
        self, vector: list[float], limit: int = 6, filters: dict[str, Any] | None = None
    ) -> list[VectorHit]:
        result = await self._run("search", vector, limit=limit, filters=filters)
        return list(result)

    async def delete(self, doc_id: str) -> bool:
        return bool(await self._run("delete", doc_id))

    async def count(self) -> int:
        return int(await self._run("count"))

    async def close(self) -> None:
        await self._primary.close()
        await self._fallback.close()

    @property
    def degraded(self) -> bool:
        return self._degraded


def build_vector_store(settings: Settings | None = None, dim: int = 512) -> VectorStore:
    settings = settings or get_settings()
    local = LocalVectorStore()
    if settings.has_zilliz:
        try:
            return FallbackVectorStore(ZillizVectorStore(settings, dim), local)
        except Exception as exc:  # noqa: BLE001 - construction failure must not be fatal
            logger.warning("could not construct Zilliz store (%s); using local index", exc)
    return local
