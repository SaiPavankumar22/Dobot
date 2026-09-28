"""DobotGraphState — the shared state of the whole task lifecycle.

One typed dict flows through the graph. Everything a stage produces and the next stage consumes
lives here, which is what makes the run replayable: the state at any node is the complete truth
about the task at that point.
"""

from __future__ import annotations

from typing import Any, TypedDict

TaskRecordT = Any


class DobotGraphState(TypedDict, total=False):
    # inputs
    task_id: str
    message: str
    mode: str
    shadow: bool
    automation_id: str | None
    region: Any
    image_base64: str | None
    #: Attachments for this message (plain dicts, so the whole state stays serialisable).
    attachments: Any
    source: str

    # stage outputs
    bundle: Any                       # ContextBundle
    route_tier: Any                   # ModelTier
    plan: Any                         # Plan
    decisions: dict[str, Any]         # step id → Decision
    verdict: str                      # plan-level verdict: allow | block
    blocked_steps: list[str]
    previews: dict[str, list[str]]

    # terminal routing
    terminal: str                     # "" | "shadow" | "blocked" | "paused" | "done" | "failed"
    failure: str


def new_state(
    *,
    task_id: str,
    message: str,
    mode: str,
    shadow: bool,
    automation_id: str | None = None,
    region: Any = None,
    image_base64: str | None = None,
    source: str = "chat",
    attachments: list[Any] | None = None,
) -> DobotGraphState:
    return DobotGraphState(
        task_id=task_id,
        message=message,
        mode=mode,
        shadow=shadow,
        automation_id=automation_id,
        region=region,
        image_base64=image_base64,
        attachments=attachments or [],
        source=source,
        terminal="",
    )


__all__ = ["DobotGraphState", "new_state", "TaskRecordT"]
