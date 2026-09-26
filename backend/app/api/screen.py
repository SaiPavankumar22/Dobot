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
from app.tools.screen import extract_screen_text, save_ephemeral

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


@router.post("/screen/context")
async def context(request: ScreenAnalyzeRequest, services: ServicesDep) -> dict:
    """Return the extracted screen context without invoking reasoning (cheap preview/dev tool)."""
    context_request = services.context_request_factory(
        message=request.question,
        region=request.region,
        image_base64=request.image,
        question=request.question,
        application=request.application,
        window_title=request.window_title,
    )
    screen = await services.context.build_screen_context(context_request)
    if screen is None:
        return {"available": False, "reason": "no image or region supplied"}
    return {"available": True, "screen": screen.model_dump(mode="json")}


@router.post("/screen/ocr")
async def ocr(request: ScreenAnalyzeRequest, services: ServicesDep) -> dict:
    """Run OCR/vision on a supplied PNG and return the text and engine used."""
    import base64

    if not request.image:
        return {"text": "", "engine": "unavailable", "reason": "no image supplied"}
    png = base64.b64decode(request.image)
    text, engine = await extract_screen_text(png, services.settings, request.question)
    return {
        "text": text,
        "engine": engine,
        "bytes": len(png),
        "image_ref": str(save_ephemeral(png, services.settings)),
    }


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
