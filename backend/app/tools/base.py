"""Tool contract.

A tool declares what it is, the risk floor it can never go below, a JSON parameter schema for the
planner prompt, how to run, and — critically — how to **verify** that it actually worked. Tools never
decide whether they are allowed to run: the decision engine does that before they are called.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.agents.sandbox import FallbackSandbox
from app.config import Settings, get_settings
from app.core.killswitch import CancellationToken
from app.events import EventBus, get_event_bus
from app.memory.store import RecordStore
from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult


@dataclass
class ToolContext:
    """Everything a tool may need, injected — no global singletons inside tool code."""

    settings: Settings = field(default_factory=get_settings)
    sandbox: FallbackSandbox | None = None
    bus: EventBus = field(default_factory=get_event_bus)
    token: CancellationToken | None = None
    task_id: str = ""
    store: RecordStore | None = None
    memory: Any = None
    approvals: Any = None
    scheduler: Any = None
    skills: Any = None
    research: Any = None
    tavily: Any = None
    hermes: Any = None
    screen: Any = None
    browser: Any = None

    def check_cancelled(self) -> None:
        if self.token:
            self.token.raise_if_cancelled()


class Tool(ABC):
    name: str = "tool"
    description: str = ""
    risk_floor: RiskLevel = RiskLevel.MEDIUM
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    #: whether the agent may use this tool without an explicit user request
    proactive_ok: bool = False

    @abstractmethod
    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult: ...

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        """Default: verify only what we can — that the call itself succeeded."""
        checks = [CheckResult(name="tool_succeeded", passed=result.ok, detail=result.error or "")]
        return VerificationResult(
            verified=result.ok,
            checks=checks,
            notes="" if result.ok else "execution reported failure",
        )

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        """Human-readable preview lines for the approval dialog.

        Sensitive tools override this so the user sees *what* is about to happen — e.g. a delete
        action listing the files it would remove, grouped by kind — rather than a tool name.
        """
        return []

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk_floor": self.risk_floor.value,
            "parameters": self.parameters,
        }

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def ok(tool: str, output: Any, started: float, **extra: Any) -> ExecutionResult:
        return ExecutionResult(
            ok=True,
            tool=tool,
            output=output,
            duration_ms=int((time.perf_counter() - started) * 1000),
            **extra,
        )

    @staticmethod
    def fail(
        tool: str, error: str, started: float, *, raw: str = "", **extra: Any
    ) -> ExecutionResult:
        return ExecutionResult(
            ok=False,
            tool=tool,
            error=error,
            raw_output=raw[:4000],
            duration_ms=int((time.perf_counter() - started) * 1000),
            **extra,
        )

    def require(self, params: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = params.get(key)
            if value not in (None, ""):
                return str(value)
        raise ValueError(f"{self.name} requires one of: {', '.join(keys)}")
