"""Memory retrieval.

Never inject every memory into every prompt. Score candidates on four signals — semantic similarity,
recency, declared importance, and relevance to the current task — and return only the best few, with
the components attached so the UI can explain *why* something was recalled.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.logging_setup import get_logger
from app.memory.embeddings import Embedder
from app.memory.store import RecordStore
from app.memory.vectors import VectorStore
from app.schemas import MemoryHit, MemoryRecord, MemoryType

logger = get_logger(__name__)


class Retriever:
    def __init__(
        self,
        *,
        vectors: VectorStore,
        embedder: Embedder,
        store: RecordStore,
        weights: dict[str, float] | None = None,
        half_life_days: float = 30.0,
        min_similarity: float = 0.15,
    ) -> None:
        self._vectors = vectors
        self._embedder = embedder
        self._store = store
        self.weights = weights or {"similarity": 0.6, "recency": 0.15, "importance": 0.15, "task": 0.1}
        self.half_life_days = half_life_days
        #: A memory has to be *meaningfully* similar before it is worth injecting. Without this gate a
        #: weak lexical overlap (or a hashing collision) can put unrelated memories into the prompt.
        self.min_similarity = min_similarity
        self._last_degraded = False

    @property
    def last_degraded(self) -> bool:
        return self._last_degraded

    def _recency(self, created_at: datetime) -> float:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age_days = max(0.0, (datetime.now(UTC) - created_at).total_seconds() / 86_400)
        return 0.5 ** (age_days / self.half_life_days)

    @staticmethod
    def _task_overlap(tags: list[str], task_tags: list[str]) -> float:
        if not task_tags:
            return 0.0
        lowered = {tag.lower() for tag in tags}
        wanted = {tag.lower() for tag in task_tags}
        if not wanted:
            return 0.0
        return len(lowered & wanted) / len(wanted)

    def score(
        self, *, similarity: float, record: MemoryRecord, task_tags: list[str] | None = None
    ) -> tuple[float, dict[str, float]]:
        components = {
            "similarity": max(0.0, min(1.0, (similarity + 1.0) / 2.0 if similarity < 0 else similarity)),
            "recency": self._recency(record.created_at),
            "importance": max(0.0, min(1.0, record.importance)),
            "task": self._task_overlap(record.tags, task_tags or []),
        }
        total = sum(self.weights.get(key, 0.0) * value for key, value in components.items())
        return total, components

    async def search(
        self,
        query: str,
        *,
        limit: int = 6,
        task_tags: list[str] | None = None,
        types: list[MemoryType] | None = None,
        min_score: float = 0.05,
    ) -> list[MemoryHit]:
        if not query.strip():
            return []
        try:
            vectors = await self._embedder.embed([query])
            self._last_degraded = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("query embedding failed (%s)", exc)
            self._last_degraded = True
            return []
        if not vectors:
            return []

        raw_hits = await self._vectors.search(vectors[0], limit=max(limit * 4, 16))
        hits: list[MemoryHit] = []
        for hit in raw_hits:
            doc = await self._store.get("memories", hit.id)
            if not doc:
                continue
            try:
                record = MemoryRecord.model_validate(doc)
            except Exception:  # noqa: BLE001 - skip malformed records rather than fail retrieval
                continue
            if types and record.type not in types:
                continue
            score, components = self.score(
                similarity=hit.score, record=record, task_tags=task_tags
            )
            if components["similarity"] < self.min_similarity and components["task"] == 0:
                continue
            if score < min_score:
                continue
            hits.append(MemoryHit(memory=record, score=round(score, 4), components=components))

        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]
