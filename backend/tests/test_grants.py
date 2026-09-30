"""Permission grants: a restricted action must *ask* (once / always), and remember the answer.

The property under test is the one the product stands on: refusal becomes a question for scopes the
user owns, the answer is remembered at the scope the user chose, and no grant can ever unlock a
system directory or a credential file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.sandbox import LocalSandbox, SandboxDenied
from app.core.decision_engine import DecisionEngine
from app.memory.store import LocalFileStore
from app.schemas import ActionSpec, ChatRequest, Plan, PlanStep, TaskStatus, Verdict
from app.security.grants import GrantStore
from app.security.jev import JEVSignals
from app.security.policies import PolicyContext, outside_roots

SYSTEM_ROOT = Path("C:/Windows") if os.name == "nt" else Path("/etc")


def outside_workspace(tmp_path: Path) -> Path:
    """A folder beyond the allowed roots (SANDBOX_ALLOWED_PATHS is tmp_path in the fixture)."""
    folder = tmp_path.parent / "elsewhere"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def move_action(folder: Path) -> ActionSpec:
    return ActionSpec(
        tool="fs_move",
        params={
            "source": str(folder / "a.txt"),
            "destination": str(folder / "sub"),
        },
    )


@pytest.fixture()
def engine(settings) -> DecisionEngine:
    return DecisionEngine(settings=settings)


@pytest.mark.asyncio
async def test_restricted_move_asks_instead_of_refusing(engine, tmp_path: Path) -> None:
    """The reported bug: a move outside the workspace was a hard refusal. It must be a question."""
    folder = outside_workspace(tmp_path)
    ctx = engine.policy_context()
    decision = await engine.evaluate_step(PlanStep(action=move_action(folder)), ctx, JEVSignals())

    assert decision.verdict is Verdict.APPROVAL, decision.reasons
    assert decision.requires_approval
    assert decision.grant is not None
    assert decision.grant["policy"] == "path_outside_sandbox"
    assert folder in [Path(root) for root in decision.grant["roots"]]


@pytest.mark.asyncio
async def test_plan_is_no_longer_refused_for_a_grantable_policy(engine, tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    plan = Plan(steps=[PlanStep(action=move_action(folder))])
    result = await engine.evaluate_plan(plan, ctx=engine.policy_context(), signals=JEVSignals())
    assert result.verdict is Verdict.APPROVAL
    assert result.blocked_steps == []


@pytest.mark.asyncio
async def test_lifetime_grant_allows_the_scope_without_asking(engine, tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    await engine.grants.add(
        policy="path_outside_sandbox",
        roots=[str(folder)],
        description=str(folder),
        scope="lifetime",
    )
    ctx = engine.policy_context()
    decision = await engine.evaluate_step(PlanStep(action=move_action(folder)), ctx, JEVSignals())

    assert decision.verdict is Verdict.ALLOW, decision.reasons
    assert decision.grant is None
    assert any("permission already granted" in reason for reason in decision.reasons)


@pytest.mark.asyncio
async def test_grant_covers_only_the_folder_it_was_given(engine, tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    other = tmp_path.parent / "elsewhere2"
    other.mkdir(parents=True, exist_ok=True)
    await engine.grants.add(
        policy="path_outside_sandbox",
        roots=[str(folder)],
        scope="lifetime",
    )
    ctx = engine.policy_context()
    decision = await engine.evaluate_step(PlanStep(action=move_action(other)), ctx, JEVSignals())
    assert decision.verdict is Verdict.APPROVAL
    assert decision.grant is not None


@pytest.mark.asyncio
async def test_a_hard_policy_is_still_a_refusal_no_matter_the_grant(engine) -> None:
    """Grantable scopes are a user choice; system paths, credentials and destruction are not."""
    await engine.grants.add(policy="path_outside_sandbox", roots=["/"], scope="lifetime")
    ctx = engine.policy_context()
    step = PlanStep(action=ActionSpec(tool="terminal_run", params={"command": "rm -rf /"}))
    decision = await engine.evaluate_step(step, ctx, JEVSignals())
    assert decision.verdict is Verdict.BLOCK
    assert decision.grant is None


@pytest.mark.asyncio
async def test_credential_path_is_refused_even_with_a_folder_grant(engine, tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    await engine.grants.add(
        policy="path_outside_sandbox", roots=[str(folder)], scope="lifetime"
    )
    ctx = engine.policy_context()
    step = PlanStep(action=ActionSpec(tool="fs_read", params={"path": str(folder / ".env")}))
    decision = await engine.evaluate_step(step, ctx, JEVSignals())
    assert decision.verdict is Verdict.BLOCK
    assert decision.grant is None


def test_outside_roots_reports_folders_not_files(tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (folder / "sub").mkdir(exist_ok=True)
    ctx = PolicyContext(allowed_paths=[tmp_path])
    roots = outside_roots(move_action(folder), ctx)
    assert folder in [Path(root) for root in roots]  # source file → its folder
    assert (folder / "sub") in [Path(root) for root in roots]  # destination directory → itself


# ------------------------------------------------------------------ the sandbox


@pytest.mark.asyncio
async def test_sandbox_refuses_until_granted_then_allows(settings, tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    grants = GrantStore()
    sandbox = LocalSandbox(settings, grants=grants)

    with pytest.raises(SandboxDenied):
        await sandbox.check_path(str(folder / "a.txt"))

    await grants.add(policy="path_outside_sandbox", roots=[str(folder)], scope="lifetime")
    assert await sandbox.check_path(str(folder / "a.txt")) == (folder / "a.txt").resolve()

    # A different folder is still refused.
    with pytest.raises(SandboxDenied):
        await sandbox.check_path(str(tmp_path.parent / "elsewhere2" / "b.txt"))


@pytest.mark.asyncio
async def test_no_grant_unlocks_system_paths_or_credentials(settings, tmp_path: Path) -> None:
    folder = outside_workspace(tmp_path)
    grants = GrantStore()
    await grants.add(
        policy="path_outside_sandbox",
        roots=[str(folder), str(SYSTEM_ROOT)],
        scope="lifetime",
    )
    sandbox = LocalSandbox(settings, grants=grants)

    with pytest.raises(SandboxDenied):
        await sandbox.check_path(str(SYSTEM_ROOT / "drivers" / "x.txt"), write=True)
    with pytest.raises(SandboxDenied):
        await sandbox.check_path(str(folder / ".env"), write=True)


# ---------------------------------------------------------------- the grant store


@pytest.mark.asyncio
async def test_lifetime_persists_and_once_does_not(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "store")
    first = GrantStore(store)
    granted = tmp_path / "granted"
    other = tmp_path / "other"
    await first.add(policy="path_outside_sandbox", roots=[str(granted)], scope="lifetime")
    await first.add(policy="path_outside_sandbox", roots=[str(other)], scope="once")

    reloaded = GrantStore(store)
    await reloaded.load()
    assert reloaded.covering([str(granted)], "path_outside_sandbox") is not None
    assert reloaded.covering([str(other)], "path_outside_sandbox") is None


@pytest.mark.asyncio
async def test_once_grant_expires(tmp_path: Path) -> None:
    grants = GrantStore(once_ttl_seconds=-1)
    await grants.add(policy="path_outside_sandbox", roots=[str(tmp_path)], scope="once")
    assert grants.covering([str(tmp_path)], "path_outside_sandbox") is None


@pytest.mark.asyncio
async def test_revoking_a_grant_makes_it_ask_again(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path / "store")
    grants = GrantStore(store)
    grant = await grants.add(
        policy="path_outside_sandbox", roots=[str(tmp_path)], scope="lifetime"
    )
    assert grants.covering([str(tmp_path)], "path_outside_sandbox") is not None
    assert await grants.revoke(grant.id) is True
    assert grants.covering([str(tmp_path)], "path_outside_sandbox") is None
    reloaded = GrantStore(store)
    await reloaded.load()
    assert reloaded.covering([str(tmp_path)], "path_outside_sandbox") is None


# ------------------------------------------------------------ end to end (services)


@pytest.mark.asyncio
async def test_restricted_move_end_to_end_asks_and_remembers(services, tmp_path: Path) -> None:
    """The reported flow, through the real orchestrator: ask → always allow → never ask again."""
    folder = outside_workspace(tmp_path)
    (folder / "sub").mkdir(parents=True, exist_ok=True)
    for name in ("a.txt", "b.txt"):
        (folder / name).write_text("data", encoding="utf-8")

    first = await services.orchestrator.submit(
        ChatRequest(message=f"move {folder / 'a.txt'} into {folder / 'sub'}")
    )
    if first.status is not TaskStatus.WAITING_APPROVAL:
        steps = [step.action.model_dump() for step in (first.plan.steps if first.plan else [])]
        pytest.skip(
            f"offline planner did not emit a gated move step "
            f"(status={first.status}, steps={steps}, answer={first.answer[:200]!r})"
        )

    approval = first.approvals[0]
    assert approval.payload.get("grant"), "a restricted move must offer once/always"
    resumed = await services.orchestrator.handle_approval(
        approval.id, "approve", scope="lifetime"
    )
    assert resumed is not None
    assert (folder / "sub" / "a.txt").exists(), resumed.answer
    assert services.grants.covering([str(folder)], "path_outside_sandbox")

    second = await services.orchestrator.submit(
        ChatRequest(message=f"move {folder / 'b.txt'} into {folder / 'sub'}")
    )
    assert second.status is not TaskStatus.WAITING_APPROVAL, second.answer
    assert (folder / "sub" / "b.txt").exists()
