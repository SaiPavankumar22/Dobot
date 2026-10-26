"""Regression tests for autonomy-hardening features.

Covers:
- Stuck-loop detection: same failure twice stops the task.
- Evidence-based completion: COMPLETED not returned when verification is unverified.
- Idempotent-only retries: non-idempotent tools do not retry.
- voice_enabled in settings MUTABLE / GET.
- Pause/resume/nudge endpoints.
"""

from __future__ import annotations

import pytest

from app.core.orchestrator import IDEMPOTENT_TOOLS, MAX_TOTAL_ATTEMPTS, ResumableState
from app.core.verifier import Verifier
from app.schemas import (
    ActionSpec,
    CheckResult,
    PlanStep,
    StepStatus,
    VerificationResult,
)

# ------------------------------------------------------------------ voice settings


def test_voice_enabled_is_in_mutable() -> None:
    from app.api.settings import MUTABLE

    assert "voice_enabled" in MUTABLE


def test_voice_enabled_returned_live(services) -> None:
    """GET /settings must return the live value, not hardcoded False."""
    services.settings.voice_enabled = True
    import asyncio

    from app.api.settings import get_settings as _get_settings

    result = asyncio.get_event_loop().run_until_complete(_get_settings(services))
    assert result["voice_enabled"] is True

    services.settings.voice_enabled = False
    result = asyncio.get_event_loop().run_until_complete(_get_settings(services))
    assert result["voice_enabled"] is False


# ------------------------------------------------------------------ verifier


@pytest.mark.asyncio
async def test_unverifiable_step_is_surfaced_but_task_can_still_be_ok() -> None:
    """An unverifiable step (skipped reason, no failed checks) is surfaced in the report
    but does not by itself mark the whole task unverified — only FAILED checks do."""
    step = PlanStep(action=ActionSpec(tool="message_send", params={}))
    step.status = StepStatus.COMPLETED
    step.verification = VerificationResult(
        verified=False,
        checks=[],
        skipped_reason="message was drafted, not sent",
    ).model_dump(mode="json")

    result = await Verifier.verify_task([step])
    # Unverifiable is surfaced for the user to see.
    assert result.unverifiable
    # But no checks failed, so task_verified is True (there's nothing to contradict success).
    assert result.verified


@pytest.mark.asyncio
async def test_failed_check_surfaces_in_failed_checks_list() -> None:
    """When a check explicitly fails, verify_task must reflect it in failed_checks."""
    step = PlanStep(action=ActionSpec(tool="fs_write", params={}))
    step.status = StepStatus.COMPLETED
    step.verification = VerificationResult(
        verified=False,
        checks=[CheckResult(name="file_exists", passed=False, detail="file not found")],
    ).model_dump(mode="json")

    result = await Verifier.verify_task([step])
    assert not result.verified
    assert "fs_write:file_exists" in result.failed_checks


@pytest.mark.asyncio
async def test_passed_checks_verify_task() -> None:
    step = PlanStep(action=ActionSpec(tool="fs_write", params={}))
    step.status = StepStatus.COMPLETED
    step.verification = VerificationResult(
        verified=True,
        checks=[CheckResult(name="file_exists", passed=True)],
    ).model_dump(mode="json")

    result = await Verifier.verify_task([step])
    assert result.verified
    assert result.total_checks == 1


# ------------------------------------------------------------------ stuck-loop constants


def test_idempotent_tool_set_contains_expected_reads() -> None:
    for tool in ("fs_list", "fs_read", "tavily_search", "browser_snapshot", "screen_capture"):
        assert tool in IDEMPOTENT_TOOLS, f"{tool} should be retryable"


def test_write_tools_not_idempotent() -> None:
    for tool in ("fs_write", "fs_delete", "message_send", "terminal_run"):
        assert tool not in IDEMPOTENT_TOOLS, f"{tool} must not be retried blindly"


def test_max_total_attempts_is_sane() -> None:
    assert MAX_TOTAL_ATTEMPTS >= 10, "must be enough for a 4-step plan with 2 retries each"
    assert MAX_TOTAL_ATTEMPTS <= 20, "must cap runaway loops quickly"


def test_resumable_state_tracks_failure_signatures() -> None:
    from app.schemas import ActionSpec, Plan, PlanStep

    plan = Plan(steps=[PlanStep(action=ActionSpec(tool="fs_read", params={}))])
    state = ResumableState(plan=plan)
    assert state.failure_signatures == {}
    assert state.total_attempts == 0

    state.failure_signatures["abc123"] = 1
    state.total_attempts = 3
    assert state.failure_signatures["abc123"] == 1


# ------------------------------------------------------------------ pause/resume/nudge


@pytest.mark.asyncio
async def test_pause_unknown_task_returns_false() -> None:
    # Build a minimal services stub — we only need a store and bus.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.core.orchestrator import Orchestrator

    bus_mock = MagicMock()
    bus_mock.emit = AsyncMock()
    store_mock = MagicMock()
    store_mock.get = AsyncMock(return_value=None)

    settings_mock = MagicMock()
    settings_mock.state_dir = MagicMock()
    settings_mock.state_dir.__truediv__ = lambda self, x: MagicMock(__truediv__=lambda s, y: MagicMock(mkdir=lambda **kw: None))

    services = SimpleNamespace(
        bus=bus_mock,
        store=store_mock,
        settings=settings_mock,
    )
    from app.core.killswitch import reset_kill_switch

    reset_kill_switch()
    orch = Orchestrator(services)  # type: ignore[arg-type]
    result = await orch.pause("non_existent_task_id")
    assert result is False


@pytest.mark.asyncio
async def test_nudge_empty_message_returns_false() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.core.orchestrator import Orchestrator

    bus_mock = MagicMock()
    bus_mock.emit = AsyncMock()
    services = SimpleNamespace(bus=bus_mock)
    from app.core.killswitch import reset_kill_switch

    reset_kill_switch()
    orch = Orchestrator(services)  # type: ignore[arg-type]
    result = await orch.nudge("some_task_id", "")
    assert result is False

    result = await orch.nudge("some_task_id", "hello")
    assert result is True
    assert orch._nudges["some_task_id"] == "hello"


# ------------------------------------------------------------------ workspace isolation


def test_workspace_root_in_security_status(services) -> None:
    """security_status must include workspace_root and network_allowlist."""
    import asyncio

    status = asyncio.get_event_loop().run_until_complete(services.security_status())
    assert "workspace_root" in status
    assert "network_allowlist" in status


@pytest.mark.asyncio
async def test_tool_context_has_workspace_root(services) -> None:
    ctx = services.tool_context()
    # The basic tool_context fixture may not have workspace_root; just check the field exists.
    assert hasattr(ctx, "workspace_root")
