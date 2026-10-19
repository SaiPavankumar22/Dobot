"""Screen context endpoints.

The desktop client captures the selected region natively and posts the PNG here; the backend can also
capture server-side when it has a display. Nothing is captured unless this endpoint is called.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import (
    ChatRequest,
    ContextRequest,
    Region,
    ScreenAnalyzeRequest,
    ScreenAnalyzeResponse,
    new_id,
)

router = APIRouter(tags=["screen"])


@router.post("/screen/analyze", response_model=ScreenAnalyzeResponse)
async def analyze(request: ScreenAnalyzeRequest, services: ServicesDep) -> ScreenAnalyzeResponse:
    """Extract text from a selected region and (optionally) answer a question about it."""
    context_request = services.context_request_factory(
        message=request.question,
        region=request.region,
        image_base64=request.image,
        question=request.question,
        application=request.application,
        window_title=request.window_title,
    )
    screen = await services.context.build_screen_context(context_request)
    context_id = new_id("ctx")
    if screen is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "NO_SCREEN_INPUT",
                "message": "supply either a selected region or an image",
                "recoverable": True,
            },
        )

    answer = ""
    task_id: str | None = None
    if request.with_reasoning:
        response = await services.orchestrator.submit(
            ChatRequest(
                message=request.question,
                context=ContextRequest(
                    screen=True, region=request.region, image=request.image
                ),
                source="screen",
            )
        )
        answer = response.answer
        task_id = response.task_id

    return ScreenAnalyzeResponse(answer=answer, context_id=context_id, screen=screen, task_id=task_id)


@router.get("/screen/privacy")
async def privacy(services: ServicesDep) -> dict:
    return {
        "mode": services.settings.screen_capture,
        "continuous_monitoring": False,
        "captures_on": [
            "explicit region selection",
            "approved workflow",
            "user-enabled screen-aware automation",
        ],
        "retention": "ephemeral: captures live under the state directory and are not indexed into memory",
        "host_region": Region(width=0, height=0).model_dump(),
    }
