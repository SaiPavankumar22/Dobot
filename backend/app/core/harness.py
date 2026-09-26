"""Agent harness — every tool step runs through the same disciplined loop.

The orchestrator already enforces the important properties: decisions before execution, approvals
before irreversible actions, verification before success is claimed. The harness makes that loop
*structural*. Inspired by harnesses like LangChain's deep-agents, each step is driven through a
three-node state graph::

    validate ──▶ execute ──▶ finalise
        │                        ▲
        └────▶ END (refused) ────┘

* ``validate`` re-checks the step just before it runs (cancellation, tool resolution) and routes to
  END when the step must not proceed.
* ``execute`` performs the guarded tool call — exactly what the direct path would do. The harness
  never replaces a guard; it wraps one.
* ``finalise`` normalises the outcome into an :class:`ExecutionResult`, so the caller receives one
  shape whether the step ran, failed, or was refused.

LangGraph is an optional dependency: when it is importable the graph runtime drives the loop and the
whole run is one inspectable state machine; when it is not, the same three stages run inline. The
result is identical in both modes — deliberately. A structure layer that can change outcomes is a
liability, not a feature. Set ``LANGGRAPH_ENABLED=false`` to force the direct path.

Usage monitoring rides along: when LangSmith is configured (see :mod:`app.observability.langsmith`)
each stage is traced, so a run can be replayed stage by stage in the LangSmith UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from app.logging_setup import get_logger
from app.observability.langsmith import get_tracer
from app.schemas import ExecutionResult

if TYPE_CHECKING:
    from app.tools.base import Tool, ToolContext

logger = get_logger(__name__)


class HarnessState(TypedDict, total=False):
    """State flowing through the harness graph."""

    tool: str
    refused: str
    ok: bool
    error: str
    output: Any
    duration_ms: int


def langgraph_available() -> bool:
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False


def harness_engine(settings: Any = None) -> str:
    """Which engine the harness will use: ``langgraph`` or ``direct``."""
    from app.config import get_settings

    settings = settings or get_settings()
    if not settings.langgraph_enabled:
        return "direct"
    return "langgraph" if langgraph_available() else "direct"


async def run_harness(step: Any, tool: Tool, ctx: ToolContext) -> ExecutionResult:
    """Run one resolved step through the harness. Returns the tool's ExecutionResult.

    Raises whatever the underlying executor raises (TaskCancelled propagates, as the orchestrator
    expects). Never changes the *outcome* of a call — only the route it took.
    """
    tracer = get_tracer()
    if harness_engine(ctx.settings) == "langgraph":
        return await _run_via_langgraph(step, tool, ctx, tracer)
    return await _run_direct(step, tool, ctx, tracer)


def _validate(step: Any, tool: Tool | None, ctx: ToolContext) -> str:
    """Pre-flight refusals. Returns '' when the step may proceed."""
    ctx.check_cancelled()
    if tool is None:
        return f"unknown tool {step.action.tool}"
    return ""


def _refused(step: Any, reason: str) -> ExecutionResult:
    return ExecutionResult(ok=False, tool=step.action.tool, error=reason)


async def _run_direct(step: Any, tool: Tool, ctx: ToolContext, tracer: Any) -> ExecutionResult:
    """Single-pass execution with a trace span — the graph's semantics, without the graph."""
    refusal = _validate(step, tool, ctx)
    if refusal:
        return _refused(step, refusal)
    with tracer.span("harness.step", tool=step.action.tool, engine="direct"):
        return await tool.run(step.action.params, ctx)


async def _run_via_langgraph(step: Any, tool: Tool, ctx: ToolContext, tracer: Any) -> ExecutionResult:
    """Drive validate→execute→finalise through a LangGraph StateGraph."""
    from langgraph.graph import END, START, StateGraph

    async def validate(state: HarnessState) -> dict[str, Any]:
        refusal = _validate(step, tool, ctx)
        return {"refused": refusal} if refusal else {}

    async def execute(state: HarnessState) -> dict[str, Any]:
        result = await tool.run(step.action.params, ctx)
        return {
            "ok": bool(result.ok),
            "error": result.error or "",
            "output": result.output,
            "duration_ms": int(result.duration_ms or 0),
        }

    async def finalise(state: HarnessState) -> dict[str, Any]:
        # Outcome normalisation lives in the graph so the whole run is visible in one place. Rich
        # verification stays with the orchestrator's verifier, which runs right after this.
        return {}

    builder = StateGraph(HarnessState)
    builder.add_node("validate", validate)
    builder.add_node("execute", execute)
    builder.add_node("finalise", finalise)
    builder.add_edge(START, "validate")
    builder.add_conditional_edges(
        "validate",
        lambda s: "execute" if not s.get("refused") else END,
        {"execute": "execute", END: END},
    )
    builder.add_edge("execute", "finalise")
    builder.add_edge("finalise", END)
    graph = builder.compile()

    with tracer.span("harness.step", tool=step.action.tool, engine="langgraph"):
        final: HarnessState = await graph.ainvoke(
            {"tool": step.action.tool},
            config={"configurable": {"thread_id": ctx.task_id}},
        )

    if final.get("refused"):
        return _refused(step, final["refused"])
    return ExecutionResult(
        ok=bool(final.get("ok")),
        tool=step.action.tool,
        error=final.get("error") or None,
        output=final.get("output"),
        duration_ms=final.get("duration_ms") or 0,
    )
