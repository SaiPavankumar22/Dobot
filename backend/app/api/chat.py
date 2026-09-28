"""Chat endpoint — the main entry point into the agent loop."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.core.attachments import IMAGE_MIMES, TEXT_EXTENSIONS, AttachmentError, limits, prepare
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
        if request.context.attachments:
            # An attachment with no words is a legitimate request — "what does this say?" is implied.
            request.message = "Describe what is attached and answer using it."
        else:
            raise HTTPException(
                status_code=422,
                detail={"code": "EMPTY_MESSAGE", "message": "message must not be empty", "recoverable": True},
            )
    try:
        prepare(request.context.attachments)
    except AttachmentError as exc:
        # Rejected before the planner ever sees it, with the reason the UI can show verbatim.
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message, "recoverable": True},
        ) from exc
    if request.background:
        task_id = await services.orchestrator.submit_background(request)
        return ChatResponse(
            task_id=task_id,
            status=TaskStatus.IN_PROGRESS,
            answer="Started in the background. I'll notify you when it's done.",
            providers=await services.provider_status(),
        )
    return await services.orchestrator.submit(request)


@router.get("/chat/attachments")
async def attachment_limits() -> dict:
    """What can be attached to a message, and how large.

    Published so the composer pre-checks the same numbers the server enforces: a client-side limit
    that differs from the server's is just a slower error message.
    """
    return {
        "images": {**limits()["images"]},
        "files": limits()["files"],
        "max_chars_in_prompt": limits()["max_chars_in_prompt"],
        "accepted": {
            "image_types": sorted(IMAGE_MIMES),
            "text_extensions": sorted(TEXT_EXTENSIONS),
        },
    }


@router.get("/chat/tools")
async def tools(services: ServicesDep) -> dict:
    """The tool surface the planner may use, with declared risk floors."""
    registry = services.registry or default_registry()
    return {"count": len(registry), "tools": registry.specs()}
