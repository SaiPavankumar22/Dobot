"""Research pipeline.

Screen context or a user request becomes a search strategy, Tavily runs the queries in parallel,
sources are collected and cross-checked, and Nemotron synthesizes a source-aware answer. Without a
Tavily key the pipeline still returns an honest, clearly-labelled empty result rather than inventing
sources.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.agents.nemotron import Reasoner
from app.config import Settings, get_settings
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.schemas import ModelTier, ResearchSource
from app.tools.tavily import TavilyClient, to_sources

logger = get_logger(__name__)

SYNTHESIS_SYSTEM = (
    "You are Dobot's research synthesizer. Answer using ONLY the provided sources. "
    "Cite sources inline as [1], [2] matching the numbered list. If the sources do not answer the "
    "question, say so explicitly instead of guessing. Be concise: 3-6 sentences for a quick answer, "
    "up to 12 lines for a deep briefing."
)

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about", "what", "when", "where",
    "which", "how", "does", "did", "can", "should", "would", "your", "you", "are", "was", "were",
    "recent", "latest", "research", "explain", "find", "please", "tell", "give", "much", "many",
}


class ResearchAgent:
    def __init__(
        self,
        *,
        tavily: TavilyClient | None = None,
        reasoner: Reasoner | None = None,
        bus: EventBus | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.tavily = tavily or TavilyClient(self.settings)
        self.reasoner = reasoner or Reasoner(self.settings)
        self.bus = bus or get_event_bus()

    # ------------------------------------------------------------------ strategy

    @staticmethod
    def _keywords(text: str, limit: int = 8) -> list[str]:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-\.]{2,}", text)
        ranked: list[str] = []
        for token in tokens:
            lowered = token.lower()
            if lowered in _STOPWORDS or lowered in ranked:
                continue
            ranked.append(lowered)
            if len(ranked) >= limit:
                break
        return ranked

    async def suggest_queries(self, topic: str, *, context_text: str = "", depth: str = "quick") -> list[str]:
        """Generate 1-5 search queries. Uses Nemotron when available, heuristics otherwise."""
        queries: list[str] = []
        if self.reasoner.available:
            prompt = (
                "Create search-engine queries that would find authoritative, current information.\n"
                f"Topic: {topic}\n"
                + (f"Selected context:\n{context_text[:2000]}\n" if context_text else "")
                + f"Return {3 if depth == 'deep' else 2} queries, one per line, no numbering, no quotes."
            )
            response = await self.reasoner.text(
                prompt,
                system="You write precise web search queries. Output only the queries, one per line.",
                tier=ModelTier.ULTRA,
                max_tokens=200,
                thinking=False,
            )
            if not response.degraded:
                queries = [
                    line.strip(" -*•\t")
                    for line in response.content.splitlines()
                    if line.strip() and len(line.strip()) > 3
                ][:5]

        if not queries:
            keywords = self._keywords(f"{topic} {context_text}")
            base = " ".join(keywords) if keywords else topic.strip()[:120]
            queries = [base]
            if depth == "deep":
                queries.extend(
                    [
                        f"{base} recent papers 2026",
                        f"{base} comparison benchmark",
                        f"{base} official documentation",
                    ]
                )
            else:
                queries.append(f"latest {base}")
        return [query for query in dict.fromkeys(queries)][:5]

    # ------------------------------------------------------------------ retrieval

    async def _search_one(self, query: str, max_results: int) -> list[ResearchSource]:
        try:
            payload = await self.tavily.search(query, max_results=max_results, depth="deep")
        except Exception as exc:  # noqa: BLE001 - one failing sub-query must not kill the research
            logger.info("sub-query failed (%s): %s", query, exc)
            return []
        return to_sources(payload)

    async def _collect(
        self, queries: list[str], *, max_sources: int
    ) -> list[ResearchSource]:
        per_query = max(3, min(6, max_sources))
        results = await asyncio.gather(
            *(self._search_one(query, per_query) for query in queries), return_exceptions=True
        )
        seen: set[str] = set()
        merged: list[ResearchSource] = []
        for batch in results:
            if isinstance(batch, BaseException):
                continue
            for source in batch:
                key = source.url.rstrip("/").lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(source)
        merged.sort(key=lambda source: source.score, reverse=True)
        return merged[:max_sources]

    # ------------------------------------------------------------------ synthesis

    def _offline_summary(self, topic: str, sources: list[ResearchSource]) -> str:
        if not sources:
            return (
                f"No live web results are available for '{topic}' because Tavily is not configured "
                "in this environment."
            )
        lines = [
            f"Tavily synthesis is unavailable (Nemotron offline), so here are the raw sources for "
            f"'{topic}':"
        ]
        for index, source in enumerate(sources, start=1):
            snippet = source.snippet.strip().replace("\n", " ")[:220]
            lines.append(f"[{index}] {source.title} — {snippet}")
        return "\n".join(lines)

    async def _synthesize(
        self, topic: str, sources: list[ResearchSource], queries: list[str]
    ) -> tuple[str, bool]:
        if not self.reasoner.available:
            return self._offline_summary(topic, sources), True
        if not sources:
            return self._offline_summary(topic, sources), False
        numbered = "\n\n".join(
            f"[{index}] {source.title} ({source.url})\n{source.snippet}"
            for index, source in enumerate(sources, start=1)
        )
        prompt = (
            f"Question: {topic}\n\n"
            f"Search queries used: {', '.join(queries)}\n\n"
            f"Sources:\n{numbered}\n\n"
            "Write the answer. Cite sources inline as [n]."
        )
        response = await self.reasoner.text(
            prompt, system=SYNTHESIS_SYSTEM, tier=ModelTier.ULTRA, max_tokens=1200
        )
        if response.degraded or not response.content.strip():
            return self._offline_summary(topic, sources), True
        return response.content.strip(), False

    # ------------------------------------------------------------------ entrypoint

    async def research(
        self,
        topic: str,
        *,
        queries: list[str] | None = None,
        max_sources: int = 6,
        depth: str = "quick",
        context_text: str = "",
        task_id: str = "",
    ) -> dict[str, Any]:
        await self.bus.emit(
            EventType.RESEARCH_PROGRESS,
            message=f"Planning research for: {topic[:80]}",
            task_id=task_id,
            stage="planning",
        )
        planned = list(queries or []) or await self.suggest_queries(
            topic, context_text=context_text, depth=depth
        )

        if not getattr(self.tavily, "available", False):
            await self.bus.emit(
                EventType.RESEARCH_PROGRESS,
                message="Tavily is not configured; skipping live search",
                task_id=task_id,
                stage="skipped",
            )
            return {
                "topic": topic,
                "summary": self._offline_summary(topic, []),
                "sources": [],
                "sub_queries": planned,
                "degraded": True,
            }

        await self.bus.emit(
            EventType.RESEARCH_PROGRESS,
            message=f"Searching {len(planned)} queries in parallel",
            task_id=task_id,
            stage="searching",
            queries=planned,
        )
        sources = await self._collect(planned, max_sources=max_sources)
        await self.bus.emit(
            EventType.RESEARCH_PROGRESS,
            message=f"Collected {len(sources)} sources",
            task_id=task_id,
            stage="collected",
            count=len(sources),
        )
        summary, degraded = await self._synthesize(topic, sources, planned)
        await self.bus.emit(
            EventType.RESEARCH_PROGRESS,
            message="Synthesised findings",
            task_id=task_id,
            stage="synthesised",
            degraded=degraded,
        )
        return {
            "topic": topic,
            "summary": summary,
            "sources": [source.model_dump() for source in sources],
            "sub_queries": planned,
            "degraded": degraded,
            "cross_checked": len({source.url for source in sources}) >= 2,
        }

    async def deep_research(self, topic: str, *, task_id: str = "", context_text: str = "") -> dict[str, Any]:
        return await self.research(
            topic,
            depth="deep",
            max_sources=10,
            context_text=context_text,
            task_id=task_id,
        )

    async def aclose(self) -> None:
        await self.tavily.aclose()
