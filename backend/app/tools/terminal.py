"""Terminal tool.

Runs through the sandbox so cwd, allowlisted paths and timeouts are enforced, and so the same call
lands inside OpenShell when NemoClaw is configured. Destructive patterns never reach this code: the
policy engine blocks them first.
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult
from app.tools.base import Tool, ToolContext

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 900.0


class TerminalRunTool(Tool):
    name = "terminal_run"
    description = (
        "Run a shell command inside the sandboxed environment and return stdout/stderr. "
        "Use for builds, tests, version checks and repository commands. Destructive commands are "
        "refused by policy."
    )
    risk_floor = RiskLevel.MEDIUM
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Working directory (must be inside the workspace)"},
            "timeout": {"type": "number", "default": 120, "maximum": 900},
            "expect_stdout": {"type": "string", "description": "Regex the output must match to count as success"},
        },
        "required": ["command"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        command = self.require(params, "command")
        cwd = params.get("cwd")
        timeout = min(float(params.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT), MAX_TIMEOUT)
        assert ctx.sandbox is not None
        try:
            result = await ctx.sandbox.run_command(
                command, cwd=str(cwd) if cwd else None, timeout=timeout, token=ctx.token
            )
        except Exception as exc:  # noqa: BLE001 - include sandbox denials
            return self.fail(self.name, str(exc), started)

        payload: dict[str, Any] = {
            "command": command,
            "cwd": cwd,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "backend": result.backend,
        }
        if not result.ok:
            return self.fail(
                self.name,
                f"command exited with {result.exit_code}: {result.stderr.strip()[:400]}",
                started,
                raw=result.stdout,
                output=payload,
            )
        return self.ok(self.name, payload, started)

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        checks = [
            CheckResult(
                name="exit_code_zero",
                passed=output.get("exit_code") == 0 and result.ok,
                detail=f"exit={output.get('exit_code')}",
            )
        ]
        expected = params.get("expect_stdout")
        if expected:
            matched = bool(re.search(str(expected), output.get("stdout", ""), re.MULTILINE))
            checks.append(
                CheckResult(name="expected_output_present", passed=matched, detail=str(expected)[:80])
            )
        return VerificationResult(verified=all(check.passed for check in checks), checks=checks)
