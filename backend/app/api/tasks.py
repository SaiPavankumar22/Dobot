"""Task endpoints: create, list, inspect, cancel."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import ChatRequest, TaskCreateRequest, TaskRecord, TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRecord])
async def list_tasks(services: ServicesDep, status: str | None = None, limit: int = 100) -> list[TaskRecord]:
    docs = await services.store.all("tasks", limit=max(limit, 1))
    records: list[TaskRecord] = []
    for doc in docs:
        try:
            record = TaskRecord.model_validate(doc)
        except Exception:  # noqa: BLE001
            continue
        if status and record.status.value != status.upper():
            continue
        records.append(record)
    records.sort(key=lambda record: record.updated_at or record.created_at, reverse=True)
    return records[:limit]


@router.post("", response_model=TaskRecord)
async def create_task(request: TaskCreateRequest, services: ServicesDep) -> TaskRecord:
    record = TaskRecord(
        title=request.title,
        description=request.description,
        priority=request.priority,
        deadline=request.deadline,
        status=TaskStatus.PENDING,
        source="manual",
    )
    await services.store.insert("tasks", record.model_dump(mode="json"))
    return record


@router.get("/{task_id}", response_model=TaskRecord)
async def get_task(task_id: str, services: ServicesDep) -> TaskRecord:
    doc = await services.store.get("tasks", task_id)
    if not doc:
        raise HTTPException(
            status_code=404,
            detail={"code": "TASK_NOT_FOUND", "message": f"no task {task_id}", "recoverable": False},
        )
    return TaskRecord.model_validate(doc)


@router.post("/{task_id}/run", response_model=TaskRecord)
async def run_task(task_id: str, services: ServicesDep) -> TaskRecord:
    """Execute a stored task (used by the dashboard's "run now" on a pending task)."""
    doc = await services.store.get("tasks", task_id)
    if not doc:
        raise HTTPException(
            status_code=404,
            detail={"code": "TASK_NOT_FOUND", "message": f"no task {task_id}", "recoverable": False},
        )
    record = TaskRecord.model_validate(doc)
    await services.orchestrator.submit_background(
        ChatRequest(message=record.description or record.title, source="task-run")
    )
    return record


@router.post("/{task_id}/cancel", response_model=TaskRecord)
async def cancel_task(task_id: str, services: ServicesDep) -> TaskRecord:
    doc = await services.store.get("tasks", task_id)
    if not doc:
        raise HTTPException(
            status_code=404,
            detail={"code": "TASK_NOT_FOUND", "message": f"no task {task_id}", "recoverable": False},
        )
    await services.orchestrator.kill(task_id)
    doc["status"] = TaskStatus.CANCELLED.value
    doc["error"] = "cancelled by user"
    await services.store.insert("tasks", doc)
    return TaskRecord.model_validate(doc)


@router.delete("/{task_id}")
async def delete_task(task_id: str, services: ServicesDep) -> dict:
    deleted = await services.store.delete("tasks", task_id)
    return {"deleted": deleted, "task_id": task_id}
