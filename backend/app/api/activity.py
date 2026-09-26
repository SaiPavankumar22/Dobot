"""Activity timeline endpoints.

The timeline is the persisted event log, shown as-is. The debug view exposes the raw sequence for
development, matching the `[23:10:01] TASK_RECEIVED` style trace in the specification.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import ServicesDep
from app.schemas import ActivityRecord

router = APIRouter(tags=["activity"])


@router.get("/activity", response_model=list[ActivityRecord])
async def recent(services: ServicesDep, limit: int = 100) -> list[ActivityRecord]:
    return await services.activity.recent(limit=limit)


@router.get("/activity/{task_id}", response_model=list[ActivityRecord])
async def for_task(task_id: str, services: ServicesDep, limit: int = 300) -> list[ActivityRecord]:
    return await services.activity.for_task(task_id, limit=limit)


@router.get("/debug/trace")
async def trace(services: ServicesDep, limit: int = Query(60, ge=1, le=400)) -> dict:
    events = services.bus.history(limit=limit)
    return {
        "lines": [
            f"[{event.timestamp.strftime('%H:%M:%S')}] {event.type.value.upper()}"
            + (f" task={event.task_id}" if event.task_id else "")
            + (f" — {event.message}" if event.message else "")
            for event in events
        ],
        "subscribers": services.bus.subscriber_count,
    }
