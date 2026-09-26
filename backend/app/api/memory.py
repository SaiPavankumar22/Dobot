"""Memory endpoints. Everything Dobot remembers is inspectable and deletable from here."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import ServicesDep
from app.schemas import MemoryCreateRequest, MemoryHit, MemoryRecord, MemoryType

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("", response_model=list[MemoryRecord])
async def list_memories(
    services: ServicesDep,
    type: MemoryType | None = None,
    search: str = "",
    limit: int = 200,
) -> list[MemoryRecord]:
    return await services.memory.list(type=type, limit=limit, search=search)


@router.get("/stats")
async def stats(services: ServicesDep) -> dict:
    return await services.memory.stats()


@router.get("/recall", response_model=list[MemoryHit])
async def recall(services: ServicesDep, q: str = Query(..., min_length=1), limit: int = 6) -> list[MemoryHit]:
    """Show what retrieval would surface for a query — the audit view for injected context."""
    return await services.memory.recall(q, limit=limit)


@router.post("", response_model=MemoryRecord)
async def create_memory(request: MemoryCreateRequest, services: ServicesDep) -> MemoryRecord:
    return await services.memory.remember(
        request.content,
        type=request.type,
        importance=request.importance,
        tags=request.tags,
        source="manual",
    )


@router.delete("/{memory_id}")
async def forget(memory_id: str, services: ServicesDep) -> dict:
    deleted = await services.memory.forget(memory_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": "MEMORY_NOT_FOUND", "message": memory_id, "recoverable": False},
        )
    return {"deleted": True, "memory_id": memory_id}


@router.post("/prune")
async def prune(
    services: ServicesDep,
    type: MemoryType | None = None,
    older_than_days: int = 30,
) -> dict:
    removed = await services.memory.prune(type=type, older_than_days=older_than_days)
    return {"removed": removed, "older_than_days": older_than_days}
