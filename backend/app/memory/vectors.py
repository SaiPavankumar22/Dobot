"""Vector index for semantic memory.

Local-only, by design. The Zilliz/Milvus path was removed rather than left half-configured: an
optional dependency nobody switches on is a second code path that is never exercised, and this
index is plenty for one person's memories with zero infrastructure.

The index is *derived* data — every vector can be regenerated from the memory it belongs to — but it
still has to survive a restart. The retriever only ever asks the index, never the record store, so an
index that came back empty after a reboot would silently switch semantic recall off while every
other part of the system still looked healthy. Hence the deliberate flush policy below: the
transcript and the memories are the source of truth, and this file is a rebuildable cache that must
not be lost casually.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.logging_setup import get_logger

logger = get_logger(__name__)

#: Flush at most this often during a burst of writes; `close()` always flushes what is left.
SAVE_INTERVAL_SECONDS = 2.0
#: …unless this many mutations have piled up, which bounds loss on a crash to one small batch.
SAVE_AFTER_MUTATIONS = 25
#: Cosine ranking on unit vectors does not need float32's full repr; this keeps the file small.
VECTOR_DECIMALS = 5


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
    """Cosine similarity index held in memory and flushed to a local JSON file.

    Constructed with no ``path`` it is purely in-process, which is what the unit tests want.
    """

    name = "local-vectors"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._vectors: dict[str, np.ndarray] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._loaded = path is None
        self._mutations = 0
        self._last_save = 0.0

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a corrupt index must not stop the app booting
            logger.warning("could not read the vector index (%s); starting empty", exc)
            return
        for entry in payload.get("entries") or []:
            doc_id = str(entry.get("id") or "")
            vector = entry.get("vector") or []
            if not doc_id or not isinstance(vector, list) or not vector:
                continue
            array = np.asarray(vector, dtype=np.float32)
            norm = float(np.linalg.norm(array))
            self._vectors[doc_id] = array / norm if norm else array
            metadata = entry.get("metadata")
            if isinstance(metadata, dict):
                self._metadata[doc_id] = metadata

    def _write(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": 1,
            "entries": [
                {
                    "id": doc_id,
                    "vector": np.round(vector, VECTOR_DECIMALS).tolist(),
                    "metadata": self._metadata.get(doc_id, {}),
                }
                for doc_id, vector in self._vectors.items()
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Write beside the target then rename: a crash mid-write must not truncate the last
            # good index, because there is nothing else to rebuild it from until the next embed.
            temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.path)
        except Exception as exc:  # noqa: BLE001 - failing to cache is not failing to serve
            logger.warning("could not persist the vector index (%s)", exc)
            return
        self._mutations = 0
        self._last_save = time.monotonic()

    def _maybe_save(self) -> None:
        if self.path is None or self._mutations == 0:
            return
        aged_out = (time.monotonic() - self._last_save) >= SAVE_INTERVAL_SECONDS
        if self._mutations >= SAVE_AFTER_MUTATIONS or aged_out:
            self._write()

    # ------------------------------------------------------------------ VectorStore

    async def upsert(self, doc_id: str, vector: list[float], metadata: dict[str, Any]) -> None:
        self._load()
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        self._vectors[doc_id] = array / norm if norm else array
        self._metadata[doc_id] = dict(metadata)
        self._mutations += 1
        self._maybe_save()

    async def search(
        self, vector: list[float], limit: int = 6, filters: dict[str, Any] | None = None
    ) -> list[VectorHit]:
        self._load()
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
        self._load()
        self._metadata.pop(doc_id, None)
        removed = self._vectors.pop(doc_id, None) is not None
        if removed:
            self._mutations += 1
            self._maybe_save()
        return removed

    async def count(self) -> int:
        self._load()
        return len(self._vectors)

    async def close(self) -> None:
        if self._mutations:
            self._write()
        self._vectors.clear()
        self._metadata.clear()


def build_vector_store(settings: Any = None, dim: int = 512) -> VectorStore:
    """The one vector backend: a local cosine index, persisted under the state directory.

    ``dim`` is accepted because the caller learns it from the embedder; the local index infers each
    vector's dimension on insert, so it is not needed here.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return LocalVectorStore(path=settings.state_dir / "vectors.json")
