"""Security endpoints: posture, policies, and the kill switch."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.core.killswitch import get_kill_switch
from app.schemas import DotStatus

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/status")
async def status(services: ServicesDep) -> dict:
    return await services.security_status()


@router.get("/policies")
async def policies(services: ServicesDep) -> dict:
    return {"policies": services.decision.policies.summarise()}


@router.get("/grants")
async def grants(services: ServicesDep) -> dict:
    """Permissions the user granted for restricted scopes (allow once / always allow)."""
    return {"grants": services.grants.summary()}


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: str, services: ServicesDep) -> dict:
    """Revoke a remembered permission; the next action in that scope asks again."""
    revoked = await services.grants.revoke(grant_id)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail={"code": "GRANT_NOT_FOUND", "message": grant_id, "recoverable": False},
        )
    return {"revoked": grant_id, "grants": services.grants.summary()}


@router.get("/permissions")
async def permissions(services: ServicesDep) -> dict:
    """The access policy at tool-level granularity: what runs unattended, what can ask, what can deny."""
    return services.permission_matrix()


@router.get("/skills")
async def skills_scan(services: ServicesDep) -> dict:
    """Pre-activation scan reports for every skill in the library."""
    return services.skill_scan()


@router.post("/kill")
async def kill(services: ServicesDep, task_id: str | None = None) -> dict:
    """Emergency stop: cancel the active task and terminate its pending tool calls."""
    killed = await services.orchestrator.kill(task_id)
    await services.bus.set_dot_status(DotStatus.KILLED, "emergency stop")
    return {"killed": killed, "active": get_kill_switch().active_ids}


@router.get("/active")
async def active(services: ServicesDep) -> dict:
    return {"active_tasks": get_kill_switch().active_ids}
