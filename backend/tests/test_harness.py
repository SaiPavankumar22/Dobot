"""Tests for the agent harness (LangGraph-backed step loop).

The invariant under test: the harness is structural, not behavioural — whichever engine drives a
step, the caller sees the same ExecutionResult.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.core.harness import harness_engine, langgraph_available, run_harness
from app.core.killswitch import CancellationToken, TaskCancelled
from app.schemas import ExecutionResult
from app.tools.base import Tool, ToolContext


class _ProbeTool(Tool):
    name = "probe"
    description = "a tool that records how often it ran"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, params: dict, ctx: ToolContext) -> ExecutionResult:  # noqa: ANN401
        self.calls += 1
        return ExecutionResult(ok=True, tool=self.name, output={"echo": params}, duration_ms=3)


def _step(tool: str = "probe", **params: object) -> SimpleNamespace:
    return SimpleNamespace(action=SimpleNamespace(tool=tool, params=params or {"x": 1}))


def _ctx(settings: Settings) -> ToolContext:
    return ToolContext(settings=settings, token=CancellationToken(task_id="t1"), task_id="t1")


# --------------------------------------------------------------------------- engine selection


def test_langgraph_available_when_installed() -> None:
    assert langgraph_available() is True


def test_engine_follows_settings_and_install_state() -> None:
    assert harness_engine(Settings(langgraph_enabled=True)) == "langgraph"
    assert harness_engine(Settings(langgraph_enabled=False)) == "direct"


# --------------------------------------------------------------------------- outcome parity


async def test_graph_path_runs_the_tool() -> None:
    tool = _ProbeTool()
    result = await run_harness(_step(), tool, _ctx(Settings(langgraph_enabled=True)))
    assert result.ok is True
    assert result.output == {"echo": {"x": 1}}
    assert tool.calls == 1


async def test_direct_path_runs_the_tool_identically() -> None:
    tool = _ProbeTool()
    result = await run_harness(_step(), tool, _ctx(Settings(langgraph_enabled=False)))
    assert result.ok is True
    assert result.output == {"echo": {"x": 1}}
    assert tool.calls == 1


async def test_unresolved_tool_is_refused_without_running() -> None:
    """tool=None is what the caller passes when the registry lookup already failed."""
    for enabled in (True, False):
        tool = _ProbeTool()
        result = await run_harness(
            _step(tool="nope"), None, _ctx(Settings(langgraph_enabled=enabled))  # type: ignore[arg-type]
        )
        assert result.ok is False
        assert "unknown tool nope" in (result.error or "")
        assert tool.calls == 0


async def test_cancellation_propagates_on_both_engines() -> None:
    for enabled in (True, False):
        tool = _ProbeTool()
        ctx = _ctx(Settings(langgraph_enabled=enabled))
        assert ctx.token is not None
        ctx.token.cancel("stop it")
        with pytest.raises(TaskCancelled):
            await run_harness(_step(), tool, ctx)
        assert tool.calls == 0


async def test_tool_failure_passes_through_unchanged() -> None:
    class _Failing(_ProbeTool):
        async def run(self, params: dict, ctx: ToolContext) -> ExecutionResult:  # noqa: ANN401
            self.calls += 1
            return ExecutionResult(ok=False, tool=self.name, error="disk on fire")

    tool = _Failing()
    for enabled in (True, False):
        result = await run_harness(_step(), tool, _ctx(Settings(langgraph_enabled=enabled)))
        assert result.ok is False
        assert result.error == "disk on fire"
