"""Research endpoint — Tavily search, source collection and synthesis outside the chat loop."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ServicesDep
from app.schemas import ResearchRequest, ResearchResponse, ResearchSource

router = APIRouter(tags=["research"])


@router.post("/research", response_model=ResearchResponse)
async def research(request: ResearchRequest, services: ServicesDep) -> ResearchResponse:
    if not request.query.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_QUERY", "message": "query must not be empty", "recoverable": True},
        )
    result = await services.research.research(
        request.query,
        max_sources=request.max_sources,
        depth=request.depth,
    )
    return ResearchResponse(
        status="completed",
        summary=result.get("summary", ""),
        sources=[ResearchSource.model_validate(source) for source in result.get("sources", [])],
        sub_queries=result.get("sub_queries", []),
        degraded=bool(result.get("degraded")),
    )


@router.get("/research/sources")
async def last_sources(services: ServicesDep) -> dict:
    """Convenience view of the most recent research events, for the dashboard research panel."""
    events = [
        event
        for event in services.bus.history(limit=200)
        if event.type.value == "research_progress"
    ]
    return {"events": [event.model_dump(mode="json") for event in events][-20:]}
