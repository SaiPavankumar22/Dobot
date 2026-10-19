"""Generic delegation to the Hermes agent.

The `computer_*` tools are single gestures: they can click one element or type one string, which is
enough only when the plan already knows every step. Real desktop work ("open the CSV in Excel and
chart revenue by month") needs an agent that can look at the screen, decide the next action and
recover from a wrong click. That is exactly what Hermes is for, so this tool hands the goal over and
records the tool trace Hermes reports back.
"""

from __future__ import annotations

import time
from typing import Any

from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult
from app.tools.base import Tool, ToolContext


class HermesTaskTool(Tool):
    name = "hermes_run"
    risk_floor = RiskLevel.MEDIUM
    description = (
        "Delegate a complete desktop or browser task to the Hermes agent (computer_use / browser "
        "toolsets). Use this when a job needs many GUI steps, reading the screen, or recovering "
        "from a wrong click — e.g. 'open report.xlsx, add a chart of column B, save it'. Describe "
        "the goal and the observable end state; Hermes chooses the gestures."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The task in plain language, including the app and the end state",
            },
            "context": {
                "type": "string",
                "description": "Files, URLs, names or values the agent needs to do the job",
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Hermes toolsets to enable, e.g. ['computer_use', 'browser_exec']",
            },
        },
        "required": ["goal"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        runtime = ctx.hermes
        if (
            runtime is None
            or not getattr(runtime, "available", False)
            or getattr(runtime, "mode", "") == "local"
        ):
            return self.fail(
                self.name,
                "no Hermes runtime is configured — set AGENT_RUNTIME=remote and run the Hermes "
                "gateway (or AGENT_RUNTIME=cli with the hermes CLI installed)",
                started,
            )

        goal = str(params.get("goal") or "").strip()
        if not goal:
            return self.fail(self.name, "goal is required", started)
        context = str(params.get("context") or "").strip()
        toolsets = params.get("toolsets") or None
        prompt = goal if not context else f"{goal}\n\nContext:\n{context}"

        result = await runtime.run_prompt(prompt, toolsets=toolsets)
        if not result.get("ok"):
            return self.fail(self.name, str(result.get("error") or "Hermes run failed"), started)
        result["evidence"] = {
            "tool_trace": result.get("tools", []),
            "run_id": result.get("run_id", ""),
            "runtime": getattr(runtime, "name", "hermes"),
        }
        return self.ok(self.name, result, started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        """A completed run is not proof the screen changed — say what Hermes actually did."""
        output = result.output or {}
        trace = output.get("evidence", {}).get("tool_trace") or []
        checks = [
            CheckResult(name="runtime_reported_success", passed=bool(output.get("ok"))),
            CheckResult(
                name="agent_took_actions",
                passed=bool(trace),
                detail=f"{len(trace)} tool events" if trace else "Hermes reported no tool use",
            ),
        ]
        return VerificationResult(
            verified=all(check.passed for check in checks),
            checks=checks,
            notes=(
                "the trace shows which tools ran; confirm the visible result yourself for anything "
                "that changed the screen or a file"
            ),
        )


HERMES_TASK_TOOLS: list[Tool] = [HermesTaskTool()]
