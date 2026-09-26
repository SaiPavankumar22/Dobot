"""Verification engine.

Dobot never reports "Done." on the strength of its own execution log. Each tool knows how to check the
real world (does the file exist, does the page have the confirmation text, did the process appear), and
the verifier runs those checks, classifies failures, and produces the user-facing summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.schemas import (
    CheckResult,
    ExecutionResult,
    PlanStep,
    VerificationResult,
)
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry

logger = get_logger(__name__)

TRANSIENT_HINTS = re.compile(
    r"(?i)(timed? ?out|timeout|temporarily|rate limit|429|503|connection reset|"
    r"network|try again|took too long)"
)
PERMISSION_HINTS = re.compile(r"(?i)(permission denied|access is denied|not authorized|403|401|eperm)")
MISSING_HINTS = re.compile(r"(?i)(not found|no such file|does not exist|enoent|404)")
USER_HINTS = re.compile(r"(?i)(requires login|sign in|authentication|approval|captcha|2fa)")


@dataclass
class FailureAnalysis:
    recoverable: bool
    category: str
    suggestion: str = ""
    ask_user: str = ""
    retry_recommended: bool = False
    retries: int = 0


class TaskVerification(BaseModel):
    """Aggregate verification result for a whole task (serialised into the task record)."""

    verified: bool
    total_checks: int = 0
    failed_checks: list[str] = Field(default_factory=list)
    unverifiable: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def summary_line(self) -> str:
        if self.unverifiable and not self.failed_checks:
            return f"completed, but {len(self.unverifiable)} step(s) could not be independently verified"
        if self.verified:
            return f"verified ({self.total_checks} checks passed)"
        return f"{len(self.failed_checks)} verification check(s) failed"


class Verifier:
    def __init__(self, registry: ToolRegistry, bus: EventBus | None = None) -> None:
        self.registry = registry
        self.bus = bus or get_event_bus()

    async def verify_step(
        self,
        step: PlanStep,
        result: ExecutionResult,
        ctx: ToolContext,
        *,
        task_id: str = "",
    ) -> VerificationResult:
        tool = self.registry.get(step.action.tool)
        if tool is None:
            return VerificationResult(
                verified=False,
                checks=[CheckResult(name="tool_known", passed=False, detail=step.action.tool)],
                skipped_reason="unknown tool",
            )
        try:
            verification = await tool.verify(step.action.params, result, ctx)
        except Exception as exc:  # noqa: BLE001 - a verifier bug must not fake success
            logger.warning("verification failed for %s (%s)", step.action.tool, exc)
            verification = VerificationResult(
                verified=False,
                checks=[CheckResult(name="verification_error", passed=False, detail=str(exc)[:200])],
                skipped_reason="verifier raised",
            )
        await self.bus.emit(
            EventType.VERIFICATION,
            message=(
                f"{step.action.tool}: {verification.summary or verification.skipped_reason}"
                if (verification.summary or verification.skipped_reason)
                else f"{step.action.tool}: verified"
            ),
            task_id=task_id,
            step_id=step.id,
            verified=verification.verified,
            checks=[check.model_dump() for check in verification.checks],
        )
        return verification

    @staticmethod
    def analyse_failure(result: ExecutionResult) -> FailureAnalysis:
        message = f"{result.error or ''} {result.raw_output or ''}"
        if USER_HINTS.search(message):
            return FailureAnalysis(
                recoverable=True,
                category="needs_user",
                ask_user="This requires you to sign in or approve something before I can continue.",
                suggestion="wait for the user, then retry the same step",
            )
        if PERMISSION_HINTS.search(message):
            return FailureAnalysis(
                recoverable=False,
                category="permission",
                suggestion="the action is outside what the sandbox permits",
            )
        if MISSING_HINTS.search(message):
            return FailureAnalysis(
                recoverable=False,
                category="missing",
                suggestion="the target does not exist; re-check the path or URL",
            )
        if TRANSIENT_HINTS.search(message):
            return FailureAnalysis(
                recoverable=True,
                category="transient",
                suggestion="network hiccup — retry once with backoff",
                retry_recommended=True,
                retries=1,
            )
        return FailureAnalysis(
            recoverable=False,
            category="unknown",
            suggestion="report the failure and let the user decide",
        )

    @staticmethod
    async def verify_task(
        steps: list[PlanStep],
        *,
        memory_written: bool = False,
        sources: list[dict[str, Any]] | None = None,
    ) -> TaskVerification:
        failed: list[str] = []
        unverifiable: list[str] = []
        notes: list[str] = []
        total = 0
        for step in steps:
            verification = step.verification or {}
            checks = verification.get("checks") or []
            total += len(checks)
            if verification.get("skipped_reason") and not verification.get("verified"):
                unverifiable.append(f"{step.action.tool}: {verification['skipped_reason']}")
                continue
            for check in checks:
                if not check.get("passed"):
                    failed.append(f"{step.action.tool}:{check.get('name')}")
        if memory_written:
            notes.append("memory updated")
        if sources:
            notes.append(f"{len(sources)} source(s) cited")
        verified = not failed
        return TaskVerification(
            verified=verified,
            total_checks=total,
            failed_checks=failed,
            unverifiable=unverifiable,
            notes=notes,
        )
