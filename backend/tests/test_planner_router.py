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


class _DeadRuntime:
    """Answers the probe the way a stopped Hermes gateway does."""

    available = True

    async def probe(self, *, force: bool = False) -> bool:
        return False


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
        "remember that this project uses SQLite for long-term memory", ContextBundle()
    )
    assert any(step.action.tool == "remember" for step in plan.steps)
    assert plan.degraded  # offline path in tests


def test_denying_inlined_attachment_content_is_detected(planner: Planner) -> None:
    """"No description was provided" is wrong when the description IS in the bundle — and must
    keep being accepted when the vision read genuinely failed, where the denial is the truth."""
    from app.schemas import ActionSpec, AttachmentBlock, Plan, PlanStep

    read = ContextBundle(
        attachments=[
            AttachmentBlock(name="diagram.png", kind="image", mime="image/png", bytes=10, text="This image shows: boxes and arrows")
        ]
    )
    unread = ContextBundle(
        attachments=[AttachmentBlock(name="diagram.png", kind="image", mime="image/png", bytes=10, text="")]
    )
    denied = Plan(summary="I cannot describe the image because no visual description was provided.")
    honest = Plan(summary="I could not read this image: the vision provider is unavailable.")
    answered = Plan(summary="The image shows boxes and arrows connected in a cycle.")
    with_steps = Plan(
        summary="no visual description was provided; I will open the file instead",
        steps=[PlanStep(index=0, reason="look", action=ActionSpec(tool="fs_list", params={}))],
    )

    assert planner._denies_inlined_content(denied, read) is True
    # Content was never read: saying so is correct, and a retry would only invite invention.
    assert planner._denies_inlined_content(denied, unread) is False
    assert planner._denies_inlined_content(honest, read) is False
    assert planner._denies_inlined_content(answered, read) is False
    # A plan that does work is not this failure mode at all.
    assert planner._denies_inlined_content(with_steps, read) is False


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
async def test_offline_open_rule_never_passes_a_sentence_as_target(planner: Planner) -> None:
    """The regex that pulls a target out of prose used to swallow the rest of it.

    That produced app_open(target="called X in my home directory. Inside it"), which the sandbox
    then refused — a plan that could never have succeeded.
    """
    plan = await planner.plan(
        "Create a folder called dobot-test in my home directory. Inside it, write report.py, "
        "then open the folder in the file explorer.",
        ContextBundle(),
    )
    open_steps = [s for s in plan.steps if s.action.tool == "app_open"]
    for step in open_steps:
        target = str(step.action.params.get("target", ""))
        assert ". " not in target, f"prose leaked into target: {target!r}"
        assert len(target) <= 200


def test_looks_like_path_rejects_prose() -> None:
    from app.core.planner import _looks_like_path

    assert _looks_like_path("~/dobot-test")
    assert _looks_like_path("Documents")
    assert _looks_like_path("D:/All/Dobot")
    assert not _looks_like_path("called dobot-multistep-test in my home directory. Inside it")
    assert not _looks_like_path("")


def test_degenerate_payload_is_not_treated_as_a_valid_plan(planner: Planner) -> None:
    """`{\"\": \"\"}` parses as JSON but carries no plan.

    Treating it as valid used to yield an empty plan that the final-answer pass then described as
    if the work had happened.
    """
    payload: dict = {"": ""}
    recognised = {"intent", "summary", "steps", "answer", "reasoning", "research_query"}
    assert payload and not (recognised & set(payload))


@pytest.mark.asyncio
async def test_degenerate_model_output_falls_back_to_offline_rules(planner: Planner) -> None:
    from app.agents.nemotron import ModelResponse
    from app.schemas import ModelTier

    class _DegenerateReasoner:
        available = True

        async def chat(self, *args, **kwargs):
            return ModelResponse(content='{"": ""}', model="test", tier=ModelTier.SUPER)

    planner.reasoner = _DegenerateReasoner()  # type: ignore[assignment]
    plan = await planner.plan("create a file called NOTES.md with the plan", ContextBundle())
    assert plan.degraded, "a payload with no recognised key must not be treated as a real plan"
    assert plan.steps, "the offline rules should still have planned something"


@pytest.mark.asyncio
async def test_truncated_model_output_retries_with_a_larger_budget(planner: Planner) -> None:
    from app.agents.nemotron import ModelResponse
    from app.schemas import ModelTier

    seen: list[int] = []

    class _TruncatingReasoner:
        available = True

        async def chat(self, *args, **kwargs):
            seen.append(int(kwargs.get("max_tokens", 0)))
            first = len(seen) == 1
            return ModelResponse(
                content='{"steps": []}' if not first else '{"": ""}',
                model="test",
                tier=ModelTier.SUPER,
                finish_reason="length" if first else "stop",
            )

    planner.reasoner = _TruncatingReasoner()  # type: ignore[assignment]
    plan = await planner.plan("create a file called NOTES.md with the plan", ContextBundle())
    assert len(seen) == 2, "a truncated response must be retried once"
    assert seen[1] > seen[0], "the retry must have a larger token budget"
    assert not plan.degraded


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
async def test_hermes_dependent_steps_are_dropped_when_runtime_is_down(planner: Planner) -> None:
    """A plan built against a dead gateway must not ship gestures that cannot run.

    The rest of the plan still executes: Dobot's own tools do not need Hermes.
    """
    planner.runtime = _DeadRuntime()
    planner._runtime_up = await planner._runtime_available()
    assert planner._runtime_up is False
    plan = planner._from_payload(
        {
            "steps": [
                {"tool": "hermes_run", "params": {"goal": "chart column B"}},
                {"tool": "computer_click", "params": {"target": "Save"}},
                {"tool": "fs_read", "params": {"path": "~/notes.md"}},
            ]
        }
    )
    validated = planner._validate(plan, ContextBundle())
    assert [step.action.tool for step in validated.steps] == ["fs_read"]


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
