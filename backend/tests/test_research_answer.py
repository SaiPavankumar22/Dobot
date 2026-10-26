from __future__ import annotations

import pytest

from app.agents.research_agent import ResearchAgent
from app.events import EventBus


class FakeTavily:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def search(self, query: str, **kwargs: object) -> dict[str, object]:
        self.calls.append({"query": query, **kwargs})
        return {
            "answer": "Headline one explains the latest development.",
            "results": [
                {
                    "title": "Headline one",
                    "url": "https://example.com/news",
                    "content": "The latest development happened today.",
                    "score": 0.9,
                }
            ],
        }


class OfflineReasoner:
    available = False


@pytest.mark.asyncio
async def test_quick_research_returns_answer_and_uses_fast_search() -> None:
    tavily = FakeTavily()
    agent = ResearchAgent(tavily=tavily, reasoner=OfflineReasoner(), bus=EventBus())

    result = await agent.research("today's major news")

    assert "Headline one explains" in result["summary"]
    assert result["sources"][0]["url"] == "https://example.com/news"
    assert tavily.calls[0]["depth"] == "quick"
    assert tavily.calls[0]["include_answer"] is True


@pytest.mark.asyncio
async def test_identical_quick_research_is_cached() -> None:
    tavily = FakeTavily()
    agent = ResearchAgent(tavily=tavily, reasoner=OfflineReasoner(), bus=EventBus())

    first = await agent.research("today's major news")
    second = await agent.research("today's major news")

    assert first["cached"] is False
    assert second["cached"] is True
    assert len(tavily.calls) == 2
