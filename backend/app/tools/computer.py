"""Desktop control tools.

These delegate to Hermes (`computer_use` toolset, background accessibility + input dispatch). If the
Hermes runtime is not configured, they fail loudly instead of pretending to click. Verification is
deliberately conservative: a runtime's own success claim is not evidence, so the verifier records what
could and could not be independently confirmed.
"""

from __future__ import annotations

import time
from typing import Any

from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult
from app.tools.base import Tool, ToolContext


class ComputerActionTool(Tool):
    """Base for `computer_*` actions routed through the Hermes runtime."""

    action: str = "click"
    risk_floor = RiskLevel.MEDIUM
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, action: str | None = None) -> None:
        if action:
            self.action = action
            self.name = f"computer_{action}"

    @property
    def runtime_available(self) -> bool:  # pragma: no cover - overridden per instance check
        return False

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        hermes = ctx.hermes
        if hermes is None or not getattr(hermes, "available", False):
            return self.fail(
                self.name,
                "desktop control requires the Hermes runtime with the computer_use toolset "
                "(set AGENT_RUNTIME=cli and configure HERMES_COMMAND); Dobot will not simulate input",
                started,
            )
        result = await hermes.computer_action(self.action, params)
        if not result.get("ok"):
            return self.fail(self.name, str(result.get("error", "computer action failed")), started)
        return self.ok(self.name, result, started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        evidence = output.get("evidence") or {}
        checks = [
            CheckResult(name="runtime_reported_success", passed=bool(output.get("ok"))),
        ]
        if evidence.get("foreground_window"):
            checks.append(
                CheckResult(
                    name="window_state_read",
                    passed=True,
                    detail=str(evidence.get("foreground_window"))[:120],
                )
            )
            if output.get("expected_window"):
                checks.append(
                    CheckResult(
                        name="expected_window_matches",
                        passed=str(output["expected_window"]).lower()
                        in str(evidence["foreground_window"]).lower(),
                    )
                )
        else:
            checks.append(
                CheckResult(
                    name="state_readback",
                    passed=False,
                    detail="runtime returned no accessibility readback",
                )
            )
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            notes="desktop actions are verified from accessibility readback when the runtime provides it",
        )


class ComputerClickTool(ComputerActionTool):
    name = "computer_click"
    action = "click"
    description = "Click a UI element by accessibility name/role or screen coordinates via Hermes."
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "Accessible name or description of the element"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "app": {"type": "string"},
        },
    }


class ComputerTypeTool(ComputerActionTool):
    name = "computer_type"
    action = "type"
    description = "Type text into the focused field via Hermes."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "target": {"type": "string"},
            "submit": {"type": "boolean", "default": False},
        },
        "required": ["text"],
    }


class ComputerKeyTool(ComputerActionTool):
    name = "computer_key"
    action = "key"
    description = "Send a key or shortcut (e.g. ctrl+s) via Hermes."
    parameters = {
        "type": "object",
        "properties": {"keys": {"type": "string"}, "app": {"type": "string"}},
        "required": ["keys"],
    }


class ComputerScrollTool(ComputerActionTool):
    name = "computer_scroll"
    action = "scroll"
    description = "Scroll the active view up or down via Hermes."
    parameters = {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down"]},
            "amount": {"type": "integer", "default": 3},
        },
    }


class ComputerDragTool(ComputerActionTool):
    name = "computer_drag"
    action = "drag"
    description = "Drag from one point to another via Hermes."
    parameters = {
        "type": "object",
        "properties": {
            "from": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}},
            "to": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}},
        },
    }


COMPUTER_TOOLS: list[Tool] = [
    ComputerClickTool(),
    ComputerTypeTool(),
    ComputerKeyTool(),
    ComputerScrollTool(),
    ComputerDragTool(),
]
