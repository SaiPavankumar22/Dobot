"""Skill endpoints: the reusable workflows Dobot can run on request."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import ChatRequest, ChatResponse, SkillRecord

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillRecord])
async def list_skills(services: ServicesDep) -> list[SkillRecord]:
    services.skills.load(force=True)
    return services.skills.records()


@router.post("", response_model=SkillRecord)
async def create_skill(payload: dict, services: ServicesDep) -> SkillRecord:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={"code": "MISSING_NAME", "message": "skill name is required", "recoverable": True},
        )
    return services.skills.save(
        name,
        str(payload.get("body", "")),
        description=str(payload.get("description", "")),
        workflow=payload.get("workflow") or [],
        trigger=str(payload.get("trigger", "")),
        required_tools=[str(tool) for tool in (payload.get("required_tools") or [])],
        safety=str(payload.get("safety", "")),
    )


@router.post("/{name}/run", response_model=ChatResponse)
async def run_skill(name: str, services: ServicesDep, inputs: dict | None = None) -> ChatResponse:
    if services.skills.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKILL_NOT_FOUND", "message": name, "recoverable": False},
        )
    message = f"Run my '{name}' skill."
    if inputs:
        message += f" Inputs: {inputs}"
    return await services.orchestrator.submit(ChatRequest(message=message, source="skill"))


@router.delete("/{name}")
async def delete_skill(name: str, services: ServicesDep) -> dict:
    deleted = services.skills.delete(name)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKILL_NOT_FOUND", "message": name, "recoverable": False},
        )
    return {"deleted": True, "name": name}
