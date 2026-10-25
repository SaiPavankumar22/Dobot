"""Persistent chat history derived from durable task records."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import ServicesDep
from app.schemas import TaskRecord

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("/{conversation_id}", response_model=list[TaskRecord])
async def conversation_history(
    conversation_id: str,
    services: ServicesDep,
    limit: int = Query(100, ge=1, le=500),
) -> list[TaskRecord]:
    """Return persisted user requests and assistant results for one conversation."""
    records: list[TaskRecord] = []
    for doc in await services.store.all("tasks", limit=5000):
        if doc.get("conversation_id", "default") != conversation_id:
            continue
        try:
            records.append(TaskRecord.model_validate(doc))
        except Exception:  # noqa: BLE001 - ignore legacy/corrupt records
            continue
    records.sort(key=lambda record: record.created_at)
    return records[-limit:]
