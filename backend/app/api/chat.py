"""Chat endpoint — the main entry point into the agent loop."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import ChatRequest, ChatResponse, TaskStatus
from app.tools.registry import default_registry

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, services: ServicesDep) -> ChatResponse:
    """Run a request through the loop.

    With ``background=true`` the response returns immediately with a task id; progress arrives over the
    WebSocket and the result is retrievable from ``/tasks/{id}``.
    """
    if not request.message.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_MESSAGE", "message": "message must not be empty", "recoverable": True},
        )
    if request.background:
        task_id = await services.orchestrator.submit_background(request)
        return ChatResponse(
            task_id=task_id,
            status=TaskStatus.IN_PROGRESS,
            answer="Started in the background. I'll notify you when it's done.",
            providers=await services.provider_status(),
        )
    return await services.orchestrator.submit(request)


@router.get("/chat/tools")
async def tools(services: ServicesDep) -> dict:
    """The tool surface the planner may use, with declared risk floors."""
    registry = services.registry or default_registry()
    return {"count": len(registry), "tools": registry.specs()}
