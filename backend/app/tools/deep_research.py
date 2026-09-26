"""The `deep_research` tool — Dobot's planner-accessible entry to the deepagents subagent.

The planner chooses this tool like any other; the decision engine authorises it (LOW risk,
read-only); the harness wraps it. Only then does the deepagents loop run its searches.
"""

from __future__ import annotations

import time
from typing import Any

from app.agents.deep_agent import deepagents_ready, run_deep_research
from app.events import EventType
from app.logging_setup import get_logger
from app.schemas import ExecutionResult, RiskLevel
from app.tools.base import Tool, ToolContext

logger = get_logger(__name__)


class DeepResearchTool(Tool):
    name = "deep_research"
    description = (
        "Run a deep research pass on a question: an autonomous subagent performs multiple web "
        "searches, cross-checks findings and returns a cited report. Slower than tavily_search; "
        "use for questions that need several searches synthesised, not for quick lookups."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = False
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The research question"},
        },
        "required": ["question"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        question = self.require(params, "question")
        ok, why = deepagents_ready(ctx.settings)
        if not ok:
            return self.ok(
                self.name,
                {"degraded": True, "notice": f"Deep research unavailable: {why}."},
                started,
            )
        await ctx.bus.emit(
            EventType.TOOL_STARTED,
            message=f"Deep research: {question[:120]}",
            tool=self.name,
        )
        report = await run_deep_research(_ServicesShim(ctx), question)
        if not report.get("ok"):
            return self.ok(
                self.name,
                {
                    "degraded": True,
                    "notice": report.get("error") or "deep research failed",
                    "question": question,
                },
                started,
            )
        return self.ok(
            self.name,
            {
                "question": question,
                "report": report.get("answer", ""),
                "engine": report.get("engine", "deepagents"),
            },
            started,
        )


class _ServicesShim:
    """The deep_agent module wants `services.settings` and `services.tavily`; the ToolContext has
    both by another name, so adapt instead of reaching for globals."""

    def __init__(self, ctx: ToolContext) -> None:
        self.settings = ctx.settings
        self.tavily = ctx.tavily
