"""Tests for the agent harness (LangGraph-backed step loop) and the LangSmith tracer.

The invariant under test: the harness is *structural, not behavioural* — whichever engine drives a
step (state graph or direct path), the caller sees the same ExecutionResult, and refusals and
cancellations propagate exactly as the direct path would produce them. The tracer, in turn, must
never be load-bearing: a broken tracing backend can cost log lines, never task outcomes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings, reset_settings_cache
from app.core.harness import harness_engine, langgraph_available, run_harness
from app.core.killswitch import CancellationToken, TaskCancelled
from app.observability import langsmith as ls
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


# --------------------------------------------------------------------------- langsmith


def test_tracer_is_null_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    reset_settings_cache()
    ls.reset_tracer()
    try:
        tracer = ls.get_tracer()
        assert tracer.enabled is False
    finally:
        ls.reset_tracer()
        reset_settings_cache()


def test_null_span_is_inert() -> None:
    with ls._NullTracer().span("anything") as span:
        span.record(ok=True)
        span.fail("boom")


class _FakeClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[tuple[object, dict]] = []

    def create_run(self, **kw: object) -> None:
        self.created.append(kw)

    def update_run(self, run_id: object, **kw: object) -> None:
        self.updated.append((run_id, kw))


def test_span_records_name_inputs_and_outputs() -> None:
    client = _FakeClient()
    tracer = ls._LangsmithTracer(client, "dobot-test")
    with tracer.span("harness.step", tool="probe") as span:
        span.record(ok=True, duration_ms=7)
        span.fail(ValueError("bad input"))
    tracer.flush()  # submissions are background; wait for them before asserting
    assert client.created and client.created[0]["name"] == "harness.step"
    assert client.created[0]["project_name"] == "dobot-test"
    run_id, payload = client.updated[0]
    assert payload["outputs"]["ok"] is True
    assert "ValueError" in str(payload["error"])
    assert run_id is client.created[0]["id"]


def test_tracing_failures_never_break_the_task() -> None:
    class _Broken:
        def create_run(self, **kw: object) -> None:
            raise RuntimeError("network down")

        def update_run(self, *a: object, **kw: object) -> None:
            raise RuntimeError("network down")

    tracer = ls._LangsmithTracer(_Broken(), "dobot-test")
    with tracer.span("harness.step") as span:
        span.record(ok=True)
    tracer.flush()
    # Reaching this line means both create_run and update_run failures were swallowed.


def test_tracer_retires_itself_after_repeated_failures() -> None:
    calls = {"n": 0}

    class _Broken:
        def create_run(self, **kw: object) -> None:
            calls["n"] += 1
            raise RuntimeError("still down")

    tracer = ls._LangsmithTracer(_Broken(), "dobot-test")
    for _ in range(20):
        with tracer.span("harness.step") as span:
            span.record(ok=True)
    tracer.flush()
    retired_early = tracer._retired
    # After the breaker trips, further spans stop reaching the client entirely.
    with tracer.span("harness.step"):
        pass
    tracer.flush()
    assert retired_early or calls["n"] <= tracer.MAX_CONSECUTIVE_FAILURES + 1


def test_unserialisable_trace_values_are_stringified() -> None:
    assert ls._safe({"a": 1}) == {"a": 1}
    assert isinstance(ls._safe(object()), str)
    assert isinstance(ls._safe({"bad": object()}), str)
