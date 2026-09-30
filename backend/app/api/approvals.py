"""Approval centre endpoints.

Approving resumes the paused task exactly where it stopped; rejecting skips that step. Either way the
decision and its consequence are written to the activity log.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import ApprovalDecisionRequest, ApprovalRecord, ChatResponse

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalRecord])
async def list_approvals(services: ServicesDep, include_resolved: bool = False) -> list[ApprovalRecord]:
    if include_resolved:
        return await services.approvals.history(limit=100)
    return await services.approvals.pending()


@router.get("/{approval_id}", response_model=ApprovalRecord)
async def get_approval(approval_id: str, services: ServicesDep) -> ApprovalRecord:
    record = await services.approvals.get(approval_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "APPROVAL_NOT_FOUND", "message": approval_id, "recoverable": False},
        )
    return record


@router.post("/{approval_id}", response_model=ChatResponse | None)
async def decide(
    approval_id: str, request: ApprovalDecisionRequest, services: ServicesDep
) -> ChatResponse | None:
    record = await services.approvals.get(approval_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "APPROVAL_NOT_FOUND", "message": approval_id, "recoverable": False},
        )
    response = await services.orchestrator.handle_approval(
        approval_id,
        request.decision,
        note=request.note,
        edits=request.edits,
        scope=request.scope,
    )
    if response is None:  # pragma: no cover - record existed a moment ago
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPROVAL_STALE",
                "message": "the approval could not be applied",
                "recoverable": False,
            },
        )
    return response
