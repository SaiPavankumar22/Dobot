"""Verification engine: aggregation of checks and failure classification."""

from __future__ import annotations

import pytest

from app.core.verifier import Verifier
from app.schemas import (
    CheckResult,
    ExecutionResult,
    PlanStep,
    StepStatus,
    VerificationResult,
)
from app.tools.registry import default_registry


def _step(tool: str, *, verified: bool, checks: list[dict] | None = None, skipped: str = "") -> PlanStep:
    step = PlanStep(action={"tool": tool, "params": {}})  # type: ignore[arg-type]
    step.status = StepStatus.COMPLETED
    step.verification = VerificationResult(
        verified=verified,
        checks=[CheckResult.model_validate(check) for check in (checks or [])],
        skipped_reason=skipped,
    ).model_dump(mode="json")
    return step


@pytest.mark.asyncio
async def test_task_is_verified_when_all_checks_pass() -> None:
    steps = [_step("fs_write", verified=True, checks=[{"name": "file_exists", "passed": True}])]
    result = await Verifier.verify_task(steps)
    assert result.verified
    assert result.total_checks == 1
    assert "verified" in result.summary_line()


@pytest.mark.asyncio
async def test_failed_check_marks_task_unverified() -> None:
    steps = [
        _step(
            "fs_write",
            verified=False,
            checks=[{"name": "content_matches", "passed": False, "detail": "hash differs"}],
        )
    ]
    result = await Verifier.verify_task(steps)
    assert not result.verified
    assert "fs_write:content_matches" in result.failed_checks


@pytest.mark.asyncio
async def test_unverifiable_step_is_reported_not_hidden() -> None:
    steps = [_step("message_send", verified=False, checks=[], skipped="message was drafted, not sent")]
    result = await Verifier.verify_task(steps)
    assert result.verified
    assert result.unverifiable
    assert "could not be independently verified" in result.summary_line()


def test_failure_classification() -> None:
    transient = Verifier.analyse_failure(
        ExecutionResult(ok=False, tool="browser_open", error="connection reset by peer")
    )
    assert transient.category == "transient"
    assert transient.retry_recommended

    permission = Verifier.analyse_failure(
        ExecutionResult(ok=False, tool="fs_write", error="Permission denied")
    )
    assert permission.category == "permission"
    assert not permission.recoverable

    missing = Verifier.analyse_failure(
        ExecutionResult(ok=False, tool="fs_read", error="No such file or directory")
    )
    assert missing.category == "missing"

    needs_user = Verifier.analyse_failure(
        ExecutionResult(ok=False, tool="browser_submit", error="This page requires login to continue")
    )
    assert needs_user.category == "needs_user"
    assert needs_user.ask_user


@pytest.mark.asyncio
async def test_unknown_tool_verification_is_not_a_pass(services) -> None:
    verifier = Verifier(services.registry, services.bus)
    step = PlanStep(action={"tool": "does_not_exist", "params": {}})  # type: ignore[arg-type]
    result = await verifier.verify_step(
        step,
        ExecutionResult(ok=True, tool="does_not_exist"),
        services.tool_context(),
    )
    assert not result.verified
    assert result.checks[0].name == "tool_known"


@pytest.mark.asyncio
async def test_tool_exception_does_not_fake_success(services, monkeypatch) -> None:
    registry = default_registry()
    tool = registry.require("fs_read")
    monkeypatch.setattr(
        tool, "verify", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    verifier = Verifier(registry, services.bus)
    step = PlanStep(action={"tool": "fs_read", "params": {}})  # type: ignore[arg-type]
    result = await verifier.verify_step(
        step, ExecutionResult(ok=True, tool="fs_read"), services.tool_context()
    )
    assert not result.verified
    assert result.checks[0].name == "verification_error"
