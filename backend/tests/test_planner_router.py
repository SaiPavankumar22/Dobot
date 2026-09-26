"""Model routing and planning (offline path, which is what the deterministic demo depends on)."""

from __future__ import annotations

import pytest

from app.agents.nemotron import Reasoner
from app.core.planner import Planner
from app.core.router import ModelRouter
from app.schemas import ContextBundle, ModelTier
from app.tools.registry import default_registry


@pytest.fixture()
def router() -> ModelRouter:
    return ModelRouter()


@pytest.fixture()
def planner(settings) -> Planner:
    return Planner(reasoner=Reasoner(settings), registry=default_registry(), settings=settings)


def test_greeting_uses_light_model(router: ModelRouter) -> None:
    assert router.route("hi there").tier is ModelTier.LIGHT


def test_research_uses_ultra(router: ModelRouter) -> None:
    decision = router.route("Find recent papers about mixture of experts")
    assert decision.tier is ModelTier.ULTRA
    assert decision.requires_research


def test_planning_uses_ultra(router: ModelRouter) -> None:
    assert router.route("Every Friday at 6pm research AI agent releases").tier is ModelTier.ULTRA


def test_screen_context_uses_super(router: ModelRouter) -> None:
    decision = router.route("what is this?", has_screen=True)
    assert decision.tier is ModelTier.SUPER


@pytest.mark.asyncio
async def test_remember_produces_memory_step(planner: Planner) -> None:
    plan = await planner.plan(
        "remember that this project uses Zilliz for long-term memory", ContextBundle()
    )
    assert any(step.action.tool == "remember" for step in plan.steps)
    assert plan.degraded  # offline path in tests


@pytest.mark.asyncio
async def test_research_request_adds_tavily_step(planner: Planner) -> None:
    plan = await planner.plan("find recent research about multimodal agents", ContextBundle())
    assert plan.needs_research
    assert any(step.action.tool.startswith("tavily") for step in plan.steps)


@pytest.mark.asyncio
async def test_create_file_produces_write_step(planner: Planner) -> None:
    plan = await planner.plan("create a file called NOTES.md with the plan", ContextBundle())
    step = next(step for step in plan.steps if step.action.tool == "fs_write")
    assert "NOTES.md" in str(step.action.params.get("path"))


@pytest.mark.asyncio
async def test_clean_downloads_prefers_moving_over_deleting(planner: Planner) -> None:
    plan = await planner.plan("clean my downloads folder", ContextBundle())
    tools = [step.action.tool for step in plan.steps]
    assert "fs_list" in tools
    assert "fs_move" in tools
    assert "fs_delete" not in tools


@pytest.mark.asyncio
async def test_delete_request_is_planned_but_approval_bound(planner: Planner) -> None:
    plan = await planner.plan("delete all files in my downloads folder", ContextBundle())
    assert any(step.action.tool == "fs_delete" for step in plan.steps)


@pytest.mark.asyncio
async def test_scheduling_request_produces_automation(planner: Planner) -> None:
    plan = await planner.plan("every friday research new AI agent releases", ContextBundle())
    step = next(step for step in plan.steps if step.action.tool == "automation_create")
    assert step.action.params["schedule"] == "0 18 * * 5"


@pytest.mark.asyncio
async def test_unknown_tools_are_dropped(planner: Planner) -> None:
    plan = planner._from_payload(
        {
            "intent": "test",
            "steps": [
                {"tool": "launch_missiles", "params": {}},
                {"tool": "fs_read", "params": {"path": "~/notes.md"}},
            ],
        }
    )
    validated = planner._validate(plan, ContextBundle())
    assert [step.action.tool for step in validated.steps] == ["fs_read"]
    assert "launch_missiles" in validated.reasoning


@pytest.mark.asyncio
async def test_duplicate_steps_are_deduplicated(planner: Planner) -> None:
    plan = planner._from_payload(
        {
            "steps": [
                {"tool": "fs_read", "params": {"path": "~/a.md"}},
                {"tool": "fs_read", "params": {"path": "~/a.md"}},
            ]
        }
    )
    validated = planner._validate(plan, ContextBundle())
    assert len(validated.steps) == 1


@pytest.mark.asyncio
async def test_memory_writes_become_remember_steps(planner: Planner) -> None:
    plan = planner._from_payload(
        {"intent": "chat", "memory_writes": ["The user prefers Python"], "steps": []}
    )
    validated = planner._validate(plan, ContextBundle())
    assert validated.steps[0].action.tool == "remember"
    assert "Python" in validated.steps[0].action.params["content"]
