"""Automation endpoints: recurring schedules and reminders."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import AutomationCreateRequest, AutomationRecord

router = APIRouter(prefix="/automations", tags=["automations"])


@router.get("", response_model=list[AutomationRecord])
async def list_automations(services: ServicesDep) -> list[AutomationRecord]:
    return await services.scheduler.list()


@router.post("", response_model=AutomationRecord)
async def create_automation(
    request: AutomationCreateRequest, services: ServicesDep
) -> AutomationRecord:
    try:
        return await services.scheduler.create_automation(
            name=request.name,
            schedule=request.schedule,
            prompt=request.task or request.name,
            kind=request.kind,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SCHEDULE", "message": str(exc), "recoverable": True},
        ) from exc


@router.post("/{automation_id}/run")
async def run_automation(automation_id: str, services: ServicesDep) -> dict:
    try:
        await services.scheduler.run_now(automation_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "AUTOMATION_NOT_FOUND", "message": automation_id, "recoverable": False},
        ) from exc
    return {"started": True, "automation_id": automation_id}


@router.patch("/{automation_id}", response_model=AutomationRecord)
async def update_automation(automation_id: str, payload: dict, services: ServicesDep) -> AutomationRecord:
    status = str(payload.get("status", "")).lower()
    if status not in {"active", "paused"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_STATUS",
                "message": "status must be 'active' or 'paused'",
                "recoverable": True,
            },
        )
    record = await services.scheduler.set_status(automation_id, status)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "AUTOMATION_NOT_FOUND", "message": automation_id, "recoverable": False},
        )
    return record


@router.delete("/{automation_id}")
async def delete_automation(automation_id: str, services: ServicesDep) -> dict:
    deleted = await services.scheduler.delete(automation_id)
    return {"deleted": deleted, "automation_id": automation_id}
