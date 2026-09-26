"""Memory: writing, retrieval ranking, forgetting and pruning."""

from __future__ import annotations

import pytest

from app.memory.embeddings import HashingEmbedder
from app.memory.manager import MemoryManager
from app.memory.retriever import Retriever
from app.memory.store import build_record_store
from app.memory.vectors import LocalVectorStore
from app.schemas import MemoryType


@pytest.fixture()
async def memory(settings, bus) -> MemoryManager:
    store = await build_record_store(settings)
    return MemoryManager(
        store=store, vectors=LocalVectorStore(), embedder=HashingEmbedder(), bus=bus
    )


@pytest.mark.asyncio
async def test_remember_then_recall(memory: MemoryManager) -> None:
    record = await memory.remember(
        "Dobot uses Zilliz for long-term memory", type=MemoryType.FACT, importance=0.8
    )
    hits = await memory.recall("Zilliz long-term memory", limit=3)
    assert hits
    assert hits[0].memory.id == record.id
    assert hits[0].score > 0.2
    assert hits[0].components["similarity"] > 0


@pytest.mark.asyncio
async def test_recall_ignores_unrelated_memories(memory: MemoryManager) -> None:
    await memory.remember("The user prefers Python for backend work", type=MemoryType.PREFERENCE)
    hits = await memory.recall("Zilliz long-term memory vector database", limit=5)
    assert all("Python" not in hit.memory.content for hit in hits)


@pytest.mark.asyncio
async def test_task_tags_boost_relevance(memory: MemoryManager) -> None:
    tagged = await memory.remember(
        "Weekly report workflow: check GitHub then Jira", type=MemoryType.WORKFLOW, tags=["report"]
    )
    await memory.remember("The user likes dark mode editors", type=MemoryType.PREFERENCE)
    hits = await memory.recall("weekly report workflow", limit=4, task_tags=["report"])
    assert hits
    assert any(hit.memory.id == tagged.id and hit.components["task"] > 0 for hit in hits)


@pytest.mark.asyncio
async def test_forget_removes_record_and_vector(memory: MemoryManager) -> None:
    record = await memory.remember("Temporary fact about the Dobot hackathon", type=MemoryType.FACT)
    assert await memory.forget(record.id)
    assert await memory.get(record.id) is None
    assert await memory.recall("Dobot hackathon fact", limit=3) == []
    assert await memory.vectors.count() == 0


@pytest.mark.asyncio
async def test_prune_removes_old_episodes(memory: MemoryManager) -> None:
    await memory.remember("Old episode", type=MemoryType.EPISODE, importance=0.2)
    removed = await memory.prune(type=MemoryType.EPISODE, older_than_days=0)
    assert removed == 1
    assert await memory.list(type=MemoryType.EPISODE) == []


@pytest.mark.asyncio
async def test_stats_reports_backends(memory: MemoryManager) -> None:
    await memory.remember("A durable fact", type=MemoryType.FACT)
    stats = await memory.stats()
    assert stats["total"] == 1
    assert stats["by_type"]["fact"] == 1
    assert stats["store"]
    assert stats["vectors"]


def test_recency_decays_with_age() -> None:
    from datetime import UTC, datetime, timedelta

    retriever = Retriever(
        vectors=LocalVectorStore(), embedder=HashingEmbedder(), store=None  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    fresh = retriever._recency(now)
    month_old = retriever._recency(now - timedelta(days=30))
    assert fresh > month_old
    assert 0.4 < month_old < 0.6  # one half-life
