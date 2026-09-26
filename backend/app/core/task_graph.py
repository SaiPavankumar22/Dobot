"""The task lifecycle as a LangGraph state machine.

Dobot's whole request lifecycle — see, understand, decide, act/refuse/preview — is one graph:

    see ──▶ understand ──▶ decide ──▶ (execute | shadow | refuse)
                              │                │
                              └──▶ END (paused for approval) ◀──┘

Each node delegates to a stage helper on the orchestrator, so the graph is *routing only* — all
behaviour stays in the orchestrator, which the direct engine also drives. The direct engine (used
when langgraph is absent or ``ORCHESTRATION_ENGINE=direct``) runs the same stage helpers in the same
order with the same early-exits, so outcomes are identical by construction; the graph adds
replayability and a single inspectable structure.

The graph is compiled once per process and cached. A compiled LangGraph graph is stateless across
runs — per-run state is passed to ``ainvoke`` — so one shared compile is safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.graph_state import DobotGraphState, new_state
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.core.orchestrator import Orchestrator

logger = get_logger(__name__)


def langgraph_available() -> bool:
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False


def orchestration_engine(settings: Any) -> str:
    """``langgraph`` or ``direct`` — what the orchestrator will actually drive."""
    if not getattr(settings, "orchestration_engine_langgraph", True):
        return "direct"
    return "langgraph" if langgraph_available() else "direct"


def _terminal(state: DobotGraphState) -> str:
    return state.get("terminal") or "execute"


def build_task_graph(orchestrator: Orchestrator) -> Any:
    """Compile the lifecycle graph around an orchestrator.

    The compiled graph closes over the orchestrator, so the cache must be per-orchestrator — a
    process-level singleton would leak one test's (or one tenant's) services into another's runs.
    Callers should store the result on the orchestrator instance and reuse it there.
    """
    from langgraph.graph import END, START, StateGraph

    async def see(state: DobotGraphState) -> dict[str, Any]:
        return await orchestrator.stage_see(state)

    async def understand(state: DobotGraphState) -> dict[str, Any]:
        return await orchestrator.stage_understand(state)

    async def decide(state: DobotGraphState) -> dict[str, Any]:
        return await orchestrator.stage_decide(state)

    async def execute(state: DobotGraphState) -> dict[str, Any]:
        return await orchestrator.stage_execute(state)

    async def shadow(state: DobotGraphState) -> dict[str, Any]:
        return await orchestrator.stage_shadow(state)

    async def refuse(state: DobotGraphState) -> dict[str, Any]:
        return await orchestrator.stage_refuse(state)

    builder = StateGraph(DobotGraphState)
    builder.add_node("see", see)
    builder.add_node("understand", understand)
    builder.add_node("decide", decide)
    builder.add_node("execute", execute)
    builder.add_node("shadow", shadow)
    builder.add_node("refuse", refuse)
    builder.add_edge(START, "see")
    builder.add_edge("see", "understand")
    builder.add_edge("understand", "decide")
    builder.add_conditional_edges(
        "decide",
        _terminal,
        {"execute": "execute", "shadow": "shadow", "refuse": "refuse", "paused": END},
    )
    for node in ("execute", "shadow", "refuse"):
        builder.add_edge(node, END)
    graph = builder.compile()
    logger.info("task lifecycle graph compiled (langgraph engine)")
    return graph


def new_lifecycle_state(**kwargs: Any) -> DobotGraphState:
    return new_state(**kwargs)
