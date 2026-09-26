"""End-to-end orchestration, offline.

These are the tests that matter most: they assert the loop actually finishes work, actually pauses for
approval, actually refuses blocked actions, and never claims success it did not verify.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.schemas import ChatRequest, DotStatus, TaskStatus


def downloads() -> Path:
    path = Path(os.environ["USERPROFILE"]) / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.asyncio
async def test_answer_only_request_completes_without_actions(services) -> None:
    response = await services.orchestrator.submit(ChatRequest(message="hello there"))
    assert response.status is TaskStatus.COMPLETED
    assert response.answer
    assert response.plan is not None
    assert response.plan.steps == []


@pytest.mark.asyncio
async def test_remember_request_persists_memory(services) -> None:
    response = await services.orchestrator.submit(
        ChatRequest(message="remember that this project uses Zilliz for long-term memory")
    )
    assert response.status is TaskStatus.COMPLETED
    memories = await services.memory.list(search="Zilliz")
    assert memories
    assert any(step.verification and step.verification["verified"] for step in response.plan.steps)


@pytest.mark.asyncio
async def test_file_creation_is_executed_and_verified(services) -> None:
    home = Path(os.environ["USERPROFILE"])
    target = home / "notes.md"
    if target.exists():
        target.unlink()
    response = await services.orchestrator.submit(ChatRequest(message="create a file called notes.md"))
    assert response.status is TaskStatus.COMPLETED, response.answer
    assert target.exists()
    checks = [
        check
        for step in response.plan.steps
        for check in (step.verification or {}).get("checks", [])
    ]
    assert any(check["name"] == "content_matches" and check["passed"] for check in checks)


@pytest.mark.asyncio
async def test_research_request_degrades_cleanly_without_tavily(services) -> None:
    response = await services.orchestrator.submit(
        ChatRequest(message="find recent research about mixture of experts")
    )
    assert response.status is TaskStatus.COMPLETED
    assert any(step.action.tool.startswith("tavily") for step in response.plan.steps)
    assert "Tavily" in response.answer or "offline" in response.answer.lower()


@pytest.mark.asyncio
async def test_delete_pauses_for_approval_and_resumes_on_approve(services) -> None:
    folder = downloads()
    for name in ("shot.png", "paper.pdf", "setup.exe"):
        (folder / name).write_text("data", encoding="utf-8")

    response = await services.orchestrator.submit(
        ChatRequest(message="delete all files in my downloads folder")
    )
    assert response.status is TaskStatus.WAITING_APPROVAL
    assert response.approvals, "an approval must be raised for deletion"
    approval = response.approvals[0]
    assert approval.risk.value == "HIGH"
    assert any("file(s)" in line for line in approval.preview)
    assert sorted(item.name for item in folder.iterdir()) == ["paper.pdf", "setup.exe", "shot.png"]

    # A task paused at a gate must report its step statuses: the UI shows a plan above the approval
    # card, and an all-pending plan there would be a lie.
    paused = await services.store.get("tasks", response.task_id)
    paused_statuses = [step["status"] for step in paused["steps"]]
    assert paused_statuses, "a paused task must persist its steps"
    assert "WAITING_APPROVAL" in paused_statuses
    assert paused_statuses.count("PENDING") < len(paused_statuses), (
        "steps that already ran must not still read as pending"
    )

    resumed = await services.orchestrator.handle_approval(approval.id, "approve")
    assert resumed is not None
    assert resumed.status is TaskStatus.COMPLETED
    assert list(folder.iterdir()) == []

    finished = await services.store.get("tasks", response.task_id)
    assert {step["status"] for step in finished["steps"]} == {"COMPLETED"}


@pytest.mark.asyncio
async def test_delete_rejection_leaves_files_alone(services) -> None:
    folder = downloads()
    (folder / "keepme.txt").write_text("important", encoding="utf-8")
    response = await services.orchestrator.submit(
        ChatRequest(message="delete all files in my downloads folder")
    )
    approval = response.approvals[0]
    resumed = await services.orchestrator.handle_approval(approval.id, "reject", note="not now")
    assert resumed is not None
    assert (folder / "keepme.txt").exists()


@pytest.mark.asyncio
async def test_shadow_mode_plans_without_executing(services) -> None:
    services.settings.shadow_mode = True
    services.decision.settings.shadow_mode = True
    folder = downloads()
    (folder / "shadow.png").write_text("x", encoding="utf-8")

    response = await services.orchestrator.submit(
        ChatRequest(message="clean my downloads folder")
    )
    assert response.status is TaskStatus.WAITING_USER
    assert response.answer.startswith("SHADOW MODE")
    assert (folder / "shadow.png").exists()
    assert not (folder / "Archive").exists()


@pytest.mark.asyncio
async def test_blocked_command_stops_the_task(services) -> None:
    step_plan = services.orchestrator  # local alias for readability
    assert step_plan is not None
    from app.schemas import ActionSpec, Plan, PlanStep

    plan = Plan(steps=[PlanStep(action=ActionSpec(tool="terminal_run", params={"command": "rm -rf /"}))])
    decisions = await services.decision.evaluate_plan(
        plan, ctx=services.decision.policy_context(), task_id="t_block"
    )
    assert decisions.verdict.value == "BLOCK"


@pytest.mark.asyncio
async def test_kill_marks_task_cancelled(services) -> None:
    from app.schemas import TaskRecord

    task = TaskRecord(title="long job", description="something slow")
    await services.store.insert("tasks", task.model_dump(mode="json"))
    killed = await services.orchestrator.kill(task.id)
    assert task.id in killed
    doc = await services.store.get("tasks", task.id)
    assert doc is not None
    assert doc["status"] == TaskStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_background_task_reports_through_the_event_bus(services) -> None:
    task_id = await services.orchestrator.submit_background(ChatRequest(message="hello"))
    assert task_id
    for _ in range(50):
        import asyncio

        await asyncio.sleep(0.05)
        doc = await services.store.get("tasks", task_id)
        if doc and doc["status"] in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value}:
            break
    events = services.bus.history(limit=100, task_id=task_id)
    types = {event.type.value for event in events}
    assert "task_received" in types
    assert "completed" in types or "failed" in types


@pytest.mark.asyncio
async def test_dot_status_reflects_progress(services) -> None:
    await services.orchestrator.submit(ChatRequest(message="create a file called status.md"))
    statuses = [
        event.data.get("status")
        for event in services.bus.history(limit=200)
        if event.type.value == "dot_status"
    ]
    assert DotStatus.THINKING.value in statuses
    assert DotStatus.EXECUTING.value in statuses
    assert DotStatus.COMPLETED.value in statuses


@pytest.mark.asyncio
# --------------------------------------------------------------- execution modes
#
# One enum, three real behaviours: ask never acts, assist confirms anything beyond a read, agent is
# the specification default. The point of these tests is that "ask" is a safety property, not a label.


@pytest.mark.asyncio
async def test_ask_mode_plans_without_touching_anything(services) -> None:
    home = Path(os.environ["USERPROFILE"])
    target = home / "ask-mode-notes.md"
    if target.exists():
        target.unlink()

    response = await services.orchestrator.submit(
        ChatRequest(message="create a file called ask-mode-notes.md", mode="ask")
    )
    assert not target.exists(), "ask mode must not execute anything, however harmless it looks"
    assert "ASK MODE" in response.answer
    assert response.plan is not None and response.plan.steps, "the plan is still produced"


@pytest.mark.asyncio
async def test_assist_mode_asks_before_a_medium_write(services) -> None:
    home = Path(os.environ["USERPROFILE"])
    target = home / "assist-mode-notes.md"
    if target.exists():
        target.unlink()

    response = await services.orchestrator.submit(
        ChatRequest(message="create a file called assist-mode-notes.md", mode="assist")
    )
    assert response.status is TaskStatus.WAITING_APPROVAL, response.answer
    assert not target.exists(), "a gated write must not have happened yet"
    assert response.approvals


@pytest.mark.asyncio
async def test_agent_mode_is_still_the_default(services) -> None:
    """Omitting the mode must behave exactly as before: a reversible MEDIUM action runs."""
    home = Path(os.environ["USERPROFILE"])
    target = home / "default-mode-notes.md"
    if target.exists():
        target.unlink()

    response = await services.orchestrator.submit(
        ChatRequest(message="create a file called default-mode-notes.md")
    )
    assert response.status is TaskStatus.COMPLETED, response.answer
    assert target.exists()


@pytest.mark.asyncio
async def test_activity_timeline_records_the_run(services) -> None:
    import asyncio

    response = await services.orchestrator.submit(ChatRequest(message="hello"))
    # The response carries the timeline synchronously; the persisted log is written by an async
    # subscriber, so poll briefly for durability (this is what the dashboard's polling does).
    kinds: list[str] = []
    for _ in range(40):
        timeline = await services.activity.for_task(response.task_id)
        kinds = [record.event_type for record in timeline]
        if "completed" in kinds:
            break
        await asyncio.sleep(0.05)
    assert "task_received" in kinds
    assert "plan_created" in kinds
    assert "completed" in kinds
