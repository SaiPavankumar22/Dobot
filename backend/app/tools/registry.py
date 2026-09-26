"""Tool registry.

One place that knows every tool the agent may call. The planner prompt is built from ``specs()``, and
execution looks tools up by name — so adding a capability means registering a class, not editing the
orchestrator.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.schemas import RiskLevel
from app.tools.apps import AppOpenTool
from app.tools.base import Tool, ToolContext
from app.tools.browser import BrowserOpenTool, BrowserReadTool, BrowserSubmitTool
from app.tools.computer import COMPUTER_TOOLS
from app.tools.deep_research import DeepResearchTool
from app.tools.filesystem import (
    FsDeleteTool,
    FsListTool,
    FsMkdirTool,
    FsMoveTool,
    FsReadTool,
    FsWriteTool,
)
from app.tools.productivity import (
    AutomationCreateTool,
    MessageSendTool,
    RememberTool,
    ReminderCreateTool,
    SkillRunTool,
    TaskCreateTool,
)
from app.tools.screen import ScreenAnalyzeTool, ScreenCaptureTool
from app.tools.tavily import TavilyResearchTool, TavilySearchTool
from app.tools.terminal import TerminalRunTool


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        return tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def specs(self, *, max_risk: RiskLevel | None = None, autonomous_ok_only: bool = False) -> list[dict]:
        specs: list[dict] = []
        for tool in self.tools():
            if max_risk is not None and tool.risk_floor.rank > max_risk.rank:
                continue
            if autonomous_ok_only and not tool.proactive_ok:
                continue
            specs.append(tool.spec())
        return specs

    def prompt_section(self) -> str:
        lines: list[str] = []
        for tool in self.tools():
            params = ", ".join((tool.parameters.get("properties") or {}).keys())
            lines.append(f"- {tool.name}({params}) [{tool.risk_floor.value}]: {tool.description}")
        return "\n".join(lines)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self.tools())

    def __len__(self) -> int:
        return len(self._tools)


def default_registry() -> ToolRegistry:
    """Every built-in tool, including the computer-use family."""
    tools: list[Tool] = [
        TavilySearchTool(),
        TavilyResearchTool(),
        DeepResearchTool(),
        FsListTool(),
        FsReadTool(),
        FsWriteTool(),
        FsMkdirTool(),
        FsMoveTool(),
        FsDeleteTool(),
        TerminalRunTool(),
        BrowserOpenTool(),
        BrowserReadTool(),
        BrowserSubmitTool(),
        ScreenCaptureTool(),
        ScreenAnalyzeTool(),
        AppOpenTool(),
        RememberTool(),
        TaskCreateTool(),
        ReminderCreateTool(),
        AutomationCreateTool(),
        SkillRunTool(),
        MessageSendTool(),
        *COMPUTER_TOOLS,
    ]
    return ToolRegistry(tools)


def context_summary(ctx: ToolContext, registry: ToolRegistry) -> str:  # pragma: no cover - helper
    return f"{len(registry)} tools available; runtime={ctx.settings.agent_runtime}"
