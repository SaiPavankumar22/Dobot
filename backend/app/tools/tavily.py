"""Tavily — Dobot's web research capability.

Wraps the Tavily REST API. The API key stays in the backend process and is attached only to requests
leaving for ``tavily.com``; nothing about it is ever returned to the frontend.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.events import EventType
from app.logging_setup import get_logger
from app.schemas import (
    CheckResult,
    ExecutionResult,
    ResearchSource,
    RiskLevel,
    VerificationResult,
)
from app.tools.base import Tool, ToolContext

logger = get_logger(__name__)


class TavilyClient:
    """Thin async client over the Tavily API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return self.settings.has_tavily

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Content-Type": "application/json"}
            if self.settings.has_tavily:
                headers["Authorization"] = f"Bearer {self.settings.tavily_api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.settings.tavily_base_url.rstrip("/"),
                headers=headers,
                timeout=httpx.Timeout(45.0),
            )
        return self._client

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        depth: str = "basic",
        include_answer: bool = False,
    ) -> dict[str, Any]:
        response = await self._http().post(
            "/search",
            json={
                "query": query,
                "max_results": max_results,
                "search_depth": "advanced" if depth == "deep" else "basic",
                "include_answer": include_answer,
                "include_raw_content": False,
            },
        )
        response.raise_for_status()
        return response.json()

    async def extract(self, urls: list[str]) -> dict[str, Any]:
        response = await self._http().post("/extract", json={"urls": urls})
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def reset_http(self) -> None:
        """Drop the cached HTTP client so the next call re-reads credentials."""
        self._client = None


def to_sources(payload: dict[str, Any]) -> list[ResearchSource]:
    sources: list[ResearchSource] = []
    for item in payload.get("results", []) or []:
        sources.append(
            ResearchSource(
                title=str(item.get("title", ""))[:200],
                url=str(item.get("url", "")),
                snippet=str(item.get("content", ""))[:600],
                score=float(item.get("score", 0.0) or 0.0),
            )
        )
    return sources


class TavilySearchTool(Tool):
    name = "tavily_search"
    description = (
        "Search the web for current information. Use for news, documentation, recent papers, "
        "product comparisons, or verifying a fact. Returns titles, URLs and snippets."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        query = self.require(params, "query")
        max_results = int(params.get("max_results", 5) or 5)

        client: TavilyClient = ctx.tavily or TavilyClient(ctx.settings)
        if not getattr(client, "available", False):
            return self.ok(
                self.name,
                {
                    "query": query,
                    "sources": [],
                    "answer": "",
                    "degraded": True,
                    "notice": "Tavily is not configured, so no live results were retrieved.",
                },
                started,
            )
        try:
            await ctx.bus.emit(
                EventType.TOOL_STARTED,
                message=f"Searching the web: {query[:80]}",
                task_id=ctx.task_id,
                tool=self.name,
            )
            payload = await client.search(query, max_results=max_results)
        except httpx.HTTPError as exc:
            return self.fail(self.name, f"Tavily request failed: {exc}", started)
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, f"search failed: {exc}", started)

        sources = to_sources(payload)
        return self.ok(
            self.name,
            {
                "query": query,
                "answer": str(payload.get("answer") or "")[:4000],
                "sources": [source.model_dump() for source in sources],
                "degraded": False,
            },
            started,
        )

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        if output.get("degraded"):
            return VerificationResult(
                verified=False,
                skipped_reason="Tavily not configured",
                checks=[CheckResult(name="provider_configured", passed=False, detail="degraded")],
            )
        sources = output.get("sources") or []
        checks = [
            CheckResult(name="response_received", passed=result.ok),
            CheckResult(name="sources_found", passed=bool(sources), detail=f"{len(sources)} results"),
            CheckResult(
                name="sources_have_urls",
                passed=all(str(source.get("url", "")).startswith("http") for source in sources),
            ),
        ]
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)


class TavilyResearchTool(Tool):
    name = "tavily_research"
    description = (
        "Run multi-query web research on a topic and return the collected sources. "
        "Use when the user asks for recent research, a deep comparison, or a sourced summary."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "max_results": {"type": "integer", "default": 6},
        },
        "required": ["topic"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        topic = self.require(params, "topic")
        queries = [str(query) for query in (params.get("queries") or []) if str(query).strip()]
        max_results = int(params.get("max_results", 6) or 6)

        if ctx.research is not None:
            result = await ctx.research.research(topic, queries=queries, max_sources=max_results)
            return self.ok(
                self.name,
                {
                    "topic": topic,
                    "summary": result.get("summary", ""),
                    "sources": result.get("sources", []),
                    "sub_queries": result.get("sub_queries", []),
                    "degraded": result.get("degraded", False),
                },
                started,
            )

        client: TavilyClient = ctx.tavily or TavilyClient(ctx.settings)
        if not getattr(client, "available", False):
            return self.ok(
                self.name,
                {"topic": topic, "sources": [], "degraded": True, "summary": ""},
                started,
            )
        try:
            payload = await client.search(topic, max_results=max_results, depth="deep")
        except Exception as exc:  # noqa: BLE001
            return self.fail(self.name, f"research failed: {exc}", started)
        sources = to_sources(payload)
        return self.ok(
            self.name,
            {
                "topic": topic,
                "sources": [source.model_dump() for source in sources],
                "summary": str(payload.get("answer") or ""),
                "degraded": False,
            },
            started,
        )
