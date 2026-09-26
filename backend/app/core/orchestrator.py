"""The orchestrator: SEE → UNDERSTAND → DECIDE → ACT → VERIFY → REMEMBER.

This module owns the lifecycle of a task. It never executes anything the decision engine has not
authorized, it pauses for human approval instead of guessing, it verifies results before claiming
success, and it can be killed mid-flight.

Long-running work continues when the chat panel is closed: a task runs as an asyncio task whose only
external dependency is the event bus, so the desktop client can disconnect and reconnect freely.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.core.cost import cost_scope
from app.core.decision_engine import EXECUTION_MODES
from app.core.graph_state import DobotGraphState
from app.core.harness import run_harness
from app.core.killswitch import TaskCancelled, get_kill_switch
from app.core.task_graph import build_task_graph, new_lifecycle_state, orchestration_engine
from app.events import EventType
from app.logging_setup import get_logger
from app.schemas import (
    ActivityRecord,
    ChatRequest,
    ChatResponse,
    ContextBundle,
    Decision,
    DotStatus,
    ExecutionResult,
    MemoryType,
    Plan,
    PlanStep,
    RiskLevel,
    StepStatus,
    TaskRecord,
    TaskStatus,
    TaskStepRecord,
    Verdict,
    VerificationResult,
)
from app.security.jev import JEVSignals
from app.security.policies import PolicyContext
from app.tools.base import ToolContext

if TYPE_CHECKING:  # pragma: no cover
    from app.services import Services

logger = get_logger(__name__)

MAX_RETRIES = 1
RETRY_DELAY_SECONDS = 1.5
MAX_REPLANS = 1


class ResumableState(BaseModel):
    """Everything needed to continue a paused task after an approval, or after a restart."""

    plan: Plan
    decisions: dict[str, Decision] = {}
    executed: list[str] = field(default_factory=list)
    #: step ids the user explicitly approved or edited — they must not be gated again on resume
    granted: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    shadow: bool = False
    #: Persisted so a task resumed from an approval gate keeps the threshold it was planned under.
    mode: str = "agent"
    approval_pending: str | None = None
    rejected: list[str] = field(default_factory=list)
    replans: int = 0


@dataclass
class StepRun:
    step: PlanStep
    result: ExecutionResult
    verification: VerificationResult | None = None


def normalize_mode(value: str | None) -> str:
    """Coerce a mode string to one of ask | assist | agent, defaulting to the specification's `agent`."""
    candidate = (value or "").strip().lower()
    return candidate if candidate in EXECUTION_MODES else "agent"


def step_records(steps: list[PlanStep]) -> list[TaskStepRecord]:
    """Project plan steps into the task-record shape the API and the UI persist.

    Called on every terminal or paused state — completed, failed, blocked, shadow — so the UI can
    show which steps actually ran instead of an all-pending snapshot of the plan.
    """
    return [
        TaskStepRecord(
            sequence=step.index,
            description=step.action.description or step.action.tool,
            status=step.status,
            tool=step.action.tool,
            action=step.action.model_dump(mode="json"),
            result=step.result,
            verification=step.verification,
            error=step.error,
        )
        for step in steps
    ]


class Orchestrator:
    def __init__(self, services: Services) -> None:
        self.s = services
        self._locks: dict[str, asyncio.Lock] = {}
        self._graph: Any = None  # compiled lifecycle graph, built once per orchestrator

    # ------------------------------------------------------------------ helpers

    def _lock(self, task_id: str) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    def _tool_context(self, task_id: str, token) -> ToolContext:
        s = self.s
        return ToolContext(
            settings=s.settings,
            sandbox=s.sandbox,
            bus=s.bus,
            token=token,
            task_id=task_id,
            store=s.store,
            memory=s.memory,
            approvals=s.approvals,
            scheduler=s.scheduler,
            skills=s.skills,
            research=s.research,
            tavily=s.tavily,
            hermes=s.runtime,
            browser=s.browser,
        )

    async def _persist(self, task: TaskRecord) -> None:
        task.updated_at = datetime.now(UTC)
        await self.s.store.insert("tasks", task.model_dump(mode="json"))

    @staticmethod
    def _new_task(request: ChatRequest) -> TaskRecord:
        title = request.message.strip().splitlines()[0][:120] or "Dobot task"
        return TaskRecord(
            title=title,
            description=request.message[:2000],
            status=TaskStatus.PENDING,
            source=request.source,
            background=request.background,
        )

    def _policy_context(self, message: str, mode: str | None = None) -> PolicyContext:
        return self.s.decision.policy_context(
            user_message=message,
            mode=mode,
            blocked_skills=self.s.skills.blocked_names(),
        )

    # ------------------------------------------------------------------ entrypoints

    async def submit(self, request: ChatRequest, *, automation_id: str | None = None) -> ChatResponse:
        """Run a request to completion (or to its first approval gate) and return the result."""
        task = self._new_task(request)
        token = get_kill_switch().register(task.id, asyncio.current_task())
        try:
            await self._run(task, request, token, automation_id=automation_id)
        except (TaskCancelled, asyncio.CancelledError):
            task.status = TaskStatus.CANCELLED
            task.error = "stopped by user"
            task.answer = task.answer or "Stopped. No further actions will be taken."
            await self._persist(task)
            await self.s.bus.emit(EventType.KILLED, message="Task stopped by user", task_id=task.id)
            await self.s.bus.set_dot_status(DotStatus.KILLED, "stopped")
        finally:
            get_kill_switch().release(task.id)
            self._locks.pop(task.id, None)
        return await self._response(task)

    async def submit_background(self, request: ChatRequest, *, automation_id: str | None = None) -> str:
        """Start a task and return immediately; progress streams over the event bus."""
        task = self._new_task(request)
        task.background = True
        task.status = TaskStatus.PENDING
        await self._persist(task)

        async def runner() -> None:
            token = get_kill_switch().register(task.id, asyncio.current_task())
            try:
                await self._run(task, request, token, automation_id=automation_id)
            except (TaskCancelled, asyncio.CancelledError):
                task.status = TaskStatus.CANCELLED
                await self._persist(task)
                await self.s.bus.emit(EventType.KILLED, message="Task stopped", task_id=task.id)
            finally:
                get_kill_switch().release(task.id)

        asyncio.create_task(runner(), name=f"dobot-task-{task.id}")
        return task.id

    async def kill(self, task_id: str | None = None) -> list[str]:
        killed = get_kill_switch().kill(task_id)
        for target in killed or ([task_id] if task_id else []):
            if not target:
                continue
            doc = await self.s.store.get("tasks", target)
            if doc:
                doc["status"] = TaskStatus.CANCELLED.value
                doc["error"] = "stopped by user"
                await self.s.store.insert("tasks", doc)
        await self.s.bus.set_dot_status(DotStatus.KILLED, "stopped by user")
        return killed

    async def run_automation(self, prompt: str, automation_id: str) -> None:
        request = ChatRequest(message=prompt, background=True, source=f"automation:{automation_id}")
        await self.submit(request, automation_id=automation_id)

    # ------------------------------------------------------------------ the loop

    # ------------------------------------------------------------------ lifecycle graph
    # The whole request lifecycle is a LangGraph state machine (see app/core/task_graph.py). These
    # stage_* helpers are the graph's nodes; the direct engine drives the same helpers in the same
    # order, so both engines are behaviourally identical by construction.

    @property
    def engine(self) -> str:
        return orchestration_engine(self.s.settings)

    async def run_lifecycle(self, state: DobotGraphState) -> DobotGraphState:
        """Drive one task through the lifecycle. Graph engine when available, direct otherwise."""
        if self.engine == "langgraph":
            if self._graph is None:
                self._graph = build_task_graph(self)
            return await self._graph.ainvoke(
                state, config={"configurable": {"thread_id": state["task_id"]}}
            )
        return await self._run_direct_lifecycle(state)

    async def _run_direct_lifecycle(self, state: DobotGraphState) -> DobotGraphState:
        """Same stages, same order, same early-exits — without the graph runtime."""
        state.update(await self.stage_see(state))
        state.update(await self.stage_understand(state))
        state.update(await self.stage_decide(state))
        terminal = state.get("terminal") or "execute"
        if terminal == "paused":
            return state
        node = {"execute": self.stage_execute, "shadow": self.stage_shadow, "refuse": self.stage_refuse}.get(
            terminal, self.stage_execute
        )
        state.update(await node(state))
        return state

    async def stage_see(self, state: DobotGraphState) -> dict[str, Any]:
        """SEE — build the context bundle (screen, memories, canonical facts)."""
        s = self.s
        await s.bus.set_dot_status(DotStatus.THINKING, "understanding your request")
        context_request = s.context_request_factory(
            message=state["message"],
            region=state.get("region"),
            image_base64=state.get("image_base64"),
            question=state["message"],
        )
        bundle: ContextBundle = await s.context.build(context_request, task_id=state["task_id"])
        return {"bundle": bundle}

    async def stage_understand(self, state: DobotGraphState) -> dict[str, Any]:
        """UNDERSTAND — route to a model tier and plan."""
        s = self.s
        bundle: ContextBundle = state["bundle"]
        route = s.router.route(
            state["message"],
            context_chars=len(bundle.as_prompt_context()),
            has_screen=bool(bundle.screen and bundle.screen.ocr_text),
            is_automation=state.get("automation_id") is not None,
        )
        logger.info("task %s routed to %s (%s)", state["task_id"], route.tier.value, route.reason)

        skill_hint = ""
        matched = s.skills.matching(state["message"])
        if matched:
            skill_hint = (
                "The user may be asking for one of these saved skills. If one fits, use `skill_run` "
                f"with its name:\n{s.skills.prompt_section(matched)}"
            )
        elif s.skills.records():
            skill_hint = "Available saved skills:\n" + s.skills.prompt_section()

        with cost_scope(task_id=state["task_id"], purpose="plan"):
            plan = await s.planner.plan(
                state["message"], bundle, tier=route.tier, task_id=state["task_id"], extra_instruction=skill_hint
            )
        await s.journal.append(
            state["task_id"],
            "plan",
            plan.summary or plan.intent,
            steps=len(plan.steps),
            tier=route.tier.value,
            degraded=plan.degraded,
            tools=[step.action.tool for step in plan.steps],
        )
        return {"plan": plan, "route_tier": route.tier}

    async def _jev_signals_for(self, state: DobotGraphState) -> JEVSignals:
        bundle: ContextBundle = state["bundle"]
        return JEVSignals(
            untrusted_content="\n".join(
                filter(
                    None,
                    [
                        bundle.screen.ocr_text if bundle.screen else "",
                        *[hit.memory.content for hit in bundle.memories if hit.memory.type is MemoryType.FACT],
                    ],
                )
            ),
            user_message=state["message"],
            autonomy="autonomous" if state.get("automation_id") else "assisted",
        )

    async def stage_decide(self, state: DobotGraphState) -> dict[str, Any]:
        """DECIDE — policy + JEV verdicts per step; route to execute / shadow / refuse / paused."""
        s = self.s
        plan: Plan = state["plan"]
        decisions = await s.decision.evaluate_plan(
            plan,
            ctx=self._policy_context(state["message"], state["mode"]),
            signals=await self._jev_signals_for(state),
            task_id=state["task_id"],
        )
        update: dict[str, Any] = {
            "decisions": decisions.decisions,
            "verdict": decisions.verdict.value,
            "blocked_steps": decisions.blocked_steps,
        }
        if state.get("shadow"):
            update["terminal"] = "shadow"
        elif decisions.verdict is Verdict.BLOCK:
            update["terminal"] = "refuse"
        else:
            update["terminal"] = "execute"
        return update

    async def stage_execute(self, state: DobotGraphState) -> dict[str, Any]:
        """ACT — run the plan's steps through the resumable executor (pauses at approval gates)."""
        task = await self.s.store.get("tasks", state["task_id"])
        if task is None:
            return {"terminal": "failed", "failure": "task record disappeared"}
        task = TaskRecord.model_validate(task)
        plan: Plan = state["plan"]
        resumable = ResumableState(
            plan=plan,
            decisions=state.get("decisions") or {},
            message=state["message"],
            shadow=state.get("shadow", False),
            mode=state["mode"],
        )
        token = get_kill_switch().token(state["task_id"]) or get_kill_switch().register(state["task_id"])
        await self._execute_state(task, resumable, token, automation_id=state.get("automation_id"))
        return {"terminal": state.get("terminal") or "done"}

    async def stage_shadow(self, state: DobotGraphState) -> dict[str, Any]:
        """Shadow mode: expand skills, build previews, show the plan — execute nothing."""
        task_doc = await self.s.store.get("tasks", state["task_id"])
        if task_doc is None:
            return {"terminal": "failed", "failure": "task record disappeared"}
        task = TaskRecord.model_validate(task_doc)
        await self._shadow_stage(task, state)
        return {"terminal": "shadow"}

    async def stage_refuse(self, state: DobotGraphState) -> dict[str, Any]:
        """The plan was refused before execution."""
        task_doc = await self.s.store.get("tasks", state["task_id"])
        if task_doc is None:
            return {"terminal": "failed", "failure": "task record disappeared"}
        task = TaskRecord.model_validate(task_doc)
        await self._refuse_stage(task, state)
        return {"terminal": "refused"}

    async def _run(
        self,
        task: TaskRecord,
        request: ChatRequest,
        token,
        *,
        automation_id: str | None = None,
    ) -> None:
        s = self.s
        shadow = request.shadow if request.shadow is not None else s.settings.shadow_mode
        # Ask mode answers and shows the plan but never executes anything. It is expressed as shadow
        # internally so there is exactly one code path that cannot act, rather than two.
        mode = normalize_mode(request.mode) or normalize_mode(s.settings.execution_mode)
        if mode == "ask":
            shadow = True
        await s.bus.emit(
            EventType.TASK_RECEIVED,
            message=request.message[:200],
            task_id=task.id,
            source=request.source,
        )
        await s.journal.append(
            task.id,
            "request",
            request.message[:300],
            mode=mode,
            shadow=shadow,
            source=request.source,
        )
        task.status = TaskStatus.PLANNING
        await self._persist(task)

        # The lifecycle (see → understand → decide → act/refuse/preview) is a LangGraph state machine
        # when langgraph is installed; the direct engine drives the identical stage helpers otherwise.
        state = new_lifecycle_state(
            task_id=task.id,
            message=request.message,
            mode=mode,
            shadow=shadow,
            automation_id=automation_id,
            region=request.context.region,
            image_base64=request.context.image,
            source=request.source,
        )
        await self.run_lifecycle(state)

    # ------------------------------------------------------------------ execution

    async def _execute_state(
        self, task: TaskRecord, state: ResumableState, token, *, automation_id: str | None = None
    ) -> None:
        """Execute the unexecuted steps of a plan, pausing at the first approval gate."""
        s = self.s
        plan = state.plan
        ctx = self._tool_context(task.id, token)
        executions: list[StepRun] = []
        replans = state.replans

        for step in plan.steps:
            if step.id in state.executed:
                continue
            decision = state.decisions.get(step.id)
            if decision is None:
                decision = await s.decision.evaluate_step(
                    step, self._policy_context(state.message, state.mode), JEVSignals(user_message=state.message)
                )
                state.decisions[step.id] = decision

            # Pre-action interceptors: rules the user wrote (GENOME.md) and rules a skill ships for
            # itself, evaluated against the *live* call rather than described in a prompt. They run
            # after the policy engine and can only tighten its verdict - never loosen it - so adding a
            # rule can only ever remove capability, which is the property that makes this safe.
            intercept = s.interceptors.evaluate(step.action.tool, step.action.params)
            if intercept.fired:
                decision.reasons = [*decision.reasons, *intercept.reasons]
                if intercept.annotations:
                    decision.jev_notes = [*decision.jev_notes, *intercept.annotations]
                if intercept.verdict is Verdict.BLOCK and decision.verdict is not Verdict.BLOCK:
                    decision.verdict = Verdict.BLOCK
                    decision.risk = RiskLevel.HIGH
                    decision.blocked_reason = "; ".join(intercept.reasons) or "blocked by an interceptor rule"
                elif intercept.verdict is Verdict.APPROVAL and step.id not in state.granted:
                    decision.verdict = Verdict.APPROVAL
                    decision.requires_approval = True
                await s.bus.emit(
                    EventType.DECISION,
                    message=(
                        f"interceptor {intercept.verdict.value}: {', '.join(intercept.fired)}"
                    ),
                    task_id=task.id,
                    step_id=step.id,
                    tool=step.action.tool,
                    interceptors=intercept.as_dict(),
                )

            if decision.verdict is Verdict.BLOCK:
                step.status = StepStatus.BLOCKED
                step.error = decision.blocked_reason or "blocked by policy"
                state.executed.append(step.id)
                continue

            if decision.verdict is Verdict.APPROVAL and step.id not in state.granted:
                approval = await s.approvals.create(
                    task_id=task.id,
                    step_id=step.id,
                    action=step.action,
                    risk=decision.risk,
                    description=step.action.description or f"{step.action.tool} requires confirmation",
                    preview=await self._preview_step(step, ctx),
                    reasons=decision.reasons,
                )
                step.status = StepStatus.WAITING_APPROVAL
                step.approval_id = approval.id
                task.status = TaskStatus.WAITING_APPROVAL
                state.approval_pending = approval.id
                task.result = {
                    "plan": plan.model_dump(mode="json"),
                    "state": state.model_dump(mode="json"),
                    "pending_approval": approval.model_dump(mode="json"),
                }
                task.answer = (
                    f"I need your approval before continuing: {approval.description}\n"
                    + "\n".join(f"• {line}" for line in approval.preview)
                )
                await s.journal.append(
                    task.id,
                    "approval_required",
                    approval.description,
                    step_id=step.id,
                    tool=step.action.tool,
                    risk=decision.risk.value,
                    reasons=decision.reasons,
                )
                # Persist the mid-flight step statuses too: a task paused at a gate should report
                # exactly which steps already ran and which one is waiting.
                task.steps = step_records(plan.steps)
                await self._persist(task)
                await s.bus.set_dot_status(DotStatus.APPROVAL_REQUIRED, "waiting for your approval")
                return

            # AUTHORIZED — act
            task.status = TaskStatus.IN_PROGRESS
            await s.bus.set_dot_status(DotStatus.EXECUTING, step.action.description or step.action.tool)
            run = await self._run_step(task, step, ctx, state)
            executions.append(run)
            await s.journal.append(
                task.id,
                "tool",
                f"{step.action.tool}: {'ok' if run.result.ok else 'failed'}",
                step_id=step.id,
                tool=step.action.tool,
                ok=run.result.ok,
                risk=step.risk.value if step.risk else None,
                duration_ms=run.result.duration_ms,
                error=(run.result.error or "")[:200] or None,
            )

            if run.step.status is StepStatus.FAILED:
                await self._finalise_failure(task, plan, state, executions, replans)
                return

            state.executed.append(step.id)
            self._collect_sources(run, state)

            # A skill invocation expands into a new plan, once.
            if replans < MAX_REPLANS and isinstance(run.result.output, dict) and run.result.output.get("replan"):
                replanned = await self._replan_from_skill(task.id, run, state)
                if replanned is not None:
                    replans += 1
                    state.replans = replans
                    nested = ResumableState(
                        plan=replanned,
                        decisions={},
                        message=state.message,
                        sources=state.sources,
                        replans=replans,
                    )
                    nested.decisions = (
                        await s.decision.evaluate_plan(
                            replanned,
                            ctx=self._policy_context(state.message, state.mode),
                            signals=JEVSignals(user_message=state.message),
                            task_id=task.id,
                        )
                    ).decisions
                    await self._execute_state(task, nested, token, automation_id=automation_id)
                    state.sources = nested.sources
                    if nested.approval_pending:
                        state.approval_pending = nested.approval_pending
                        return
                    continue

            await self._persist(task)

        await self._finalise_success(task, plan, state, executions)

    async def _run_step(
        self, task: TaskRecord, step: PlanStep, ctx: ToolContext, state: ResumableState
    ) -> StepRun:
        s = self.s
        tool = s.registry.get(step.action.tool)
        if tool is None:
            step.status = StepStatus.FAILED
            step.error = f"unknown tool {step.action.tool}"
            result = ExecutionResult(ok=False, tool=step.action.tool, error=step.error)
            return StepRun(step=step, result=result)

        attempt = 0
        result: ExecutionResult | None = None
        while attempt <= MAX_RETRIES:
            ctx.check_cancelled()
            step.status = StepStatus.RUNNING
            await s.bus.emit(
                EventType.EXECUTION_STARTED,
                message=step.action.description or step.action.tool,
                task_id=task.id,
                step_id=step.id,
                tool=step.action.tool,
                risk=step.risk.value if step.risk else None,
                attempt=attempt + 1,
            )
            # The harness wraps, never replaces, the guarded call: same outcome, one structured
            # route (validate→execute→finalise) and a trace span when observability is on.
            result = await run_harness(step, tool, ctx)
            if result.ok:
                break
            analysis = s.verifier.analyse_failure(result)
            if analysis.retry_recommended and attempt < MAX_RETRIES:
                attempt += 1
                await s.bus.emit(
                    EventType.TOOL_STARTED,
                    message=f"Retrying after a transient failure: {step.action.tool}",
                    task_id=task.id,
                    tool=step.action.tool,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            break

        assert result is not None
        step.result = result.model_dump(mode="json")

        verification: VerificationResult | None = None
        if result.ok:
            verification = await s.verifier.verify_step(step, result, ctx, task_id=task.id)
            step.verification = verification.model_dump(mode="json")
            hard_failure = bool(verification.checks) and any(
                not check.passed for check in verification.checks
            ) and not verification.verified
            if hard_failure:
                step.status = StepStatus.FAILED
                step.error = f"verification failed: {verification.summary or 'checks did not pass'}"
            else:
                step.status = StepStatus.COMPLETED
        else:
            step.status = StepStatus.FAILED
            step.error = result.error or "execution failed"
            analysis = s.verifier.analyse_failure(result)
            step.decision_reason = f"{step.decision_reason} (failure: {analysis.category})".strip()

        await s.bus.emit(
            EventType.TOOL_COMPLETED,
            message=(
                f"{step.action.tool}: {'ok' if result.ok else 'failed'}"
                + (f" — {step.error}" if step.error else "")
            ),
            task_id=task.id,
            step_id=step.id,
            tool=step.action.tool,
            ok=result.ok,
            duration_ms=result.duration_ms,
            verification=step.verification,
        )
        return StepRun(step=step, result=result, verification=verification)

    # ------------------------------------------------------------------ resume

    async def handle_approval(
        self, approval_id: str, decision: str, *, note: str = "", edits: dict[str, Any] | None = None
    ) -> ChatResponse | None:
        s = self.s
        approval = await s.approvals.get(approval_id)
        if approval is None:
            return None
        record = await s.approvals.resolve(approval_id, decision, note=note, edits=edits)
        if record is None:
            return None

        doc = await s.store.get("tasks", record.task_id)
        if not doc:
            return None
        task = TaskRecord.model_validate(doc)
        state_dump = (task.result or {}).get("state")
        if not state_dump:
            return await self._response(task)
        state = ResumableState.model_validate(state_dump)
        state.approval_pending = None

        step = next((item for item in state.plan.steps if item.id == record.step_id), None)
        if step is None:
            return await self._response(task)

        token = get_kill_switch().register(task.id, asyncio.current_task())
        async with self._lock(task.id):
            if record.status.value in {"REJECTED"}:
                step.status = StepStatus.SKIPPED
                step.error = "rejected by user"
                state.rejected.append(step.id)
                state.executed.append(step.id)
                task.answer = "You rejected that action, so I skipped it."
                await self._persist(task)
                # Continue: remaining steps may be independent of the rejected one.
                await self._execute_state(task, state, token)
                if task.status not in {TaskStatus.WAITING_APPROVAL, TaskStatus.IN_PROGRESS}:
                    await self._finalise_status_after_resume(task, state)
                return await self._response(task)

            if edits:
                step.action.params = {**step.action.params, **edits}
                step.action.description = f"{step.action.description} (edited by user)"
            # Record the grant so re-entering the loop executes this step instead of re-asking.
            if step.id not in state.granted:
                state.granted.append(step.id)
            step.status = StepStatus.PENDING
            try:
                await self._execute_state(task, state, token)
            except (TaskCancelled, asyncio.CancelledError):
                task.status = TaskStatus.CANCELLED
                await self._persist(task)
            finally:
                get_kill_switch().release(task.id)
        await self._finalise_status_after_resume(task, state)
        return await self._response(task)

    async def _finalise_status_after_resume(self, task: TaskRecord, state: ResumableState) -> None:
        if task.status in {TaskStatus.WAITING_APPROVAL, TaskStatus.CANCELLED}:
            return
        if task.status is not TaskStatus.COMPLETED and task.status is not TaskStatus.FAILED:
            await self._finalise_success(task, state.plan, state, [])

    # ------------------------------------------------------------------ finishing

    async def _finalise_success(
        self, task: TaskRecord, plan: Plan, state: ResumableState, executions: list[StepRun]
    ) -> None:
        s = self.s
        steps = plan.steps
        verification = await s.verifier.verify_task(steps, sources=state.sources)
        answer = await self._compose_answer(task, plan, steps, state.sources, verification)

        if any(step.status is StepStatus.BLOCKED for step in steps):
            task.status = TaskStatus.FAILED
            task.error = "some steps were blocked by policy"
        elif any(step.status is StepStatus.FAILED for step in steps):
            task.status = TaskStatus.FAILED
            task.error = next(
                (step.error for step in steps if step.status is StepStatus.FAILED), "step failed"
            )
        else:
            task.status = TaskStatus.COMPLETED

        task.answer = answer
        task.sources = state.sources
        task.progress = 100 if task.status is TaskStatus.COMPLETED else 60
        task.steps = step_records(steps)
        task.result = {
            "plan": plan.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
            "sources": state.sources,
            "usage": s.cost.for_task(task.id) if getattr(s, "cost", None) else None,
        }
        await self._persist(task)
        await s.journal.append(
            task.id,
            "completed" if task.status is TaskStatus.COMPLETED else "failed",
            task.answer[:300],
            status=task.status.value,
            verification=verification.summary_line(),
            steps=[f"{step.action.tool}:{step.status.value}" for step in steps],
            usage=s.cost.for_task(task.id) if getattr(s, "cost", None) else None,
        )
        await self._remember_episode(task, state.message, plan, verification)

        if task.status is TaskStatus.COMPLETED:
            await s.bus.emit(
                EventType.COMPLETED,
                message=f"Completed: {task.title[:80]} ({verification.summary_line()})",
                task_id=task.id,
                verification=verification.model_dump(mode="json"),
                history=executions and [run.step.action.tool for run in executions],
            )
            await s.bus.set_dot_status(DotStatus.COMPLETED, "done")
            await s.bus.emit(
                EventType.NOTIFICATION,
                message=task.title[:120],
                task_id=task.id,
                title="Dobot finished a task",
                body=answer[:200],
                background=task.background,
            )
        else:
            await s.bus.emit(
                EventType.FAILED,
                message=f"Task failed: {task.error}",
                task_id=task.id,
            )
            await s.bus.set_dot_status(DotStatus.ERROR, task.error or "failed")

    async def _finalise_failure(
        self,
        task: TaskRecord,
        plan: Plan,
        state: ResumableState,
        executions: list[StepRun],
        replans: int,
    ) -> None:
        failed = next((run for run in executions if run.step.status is StepStatus.FAILED), None)
        remaining = [step for step in plan.steps if step.status is StepStatus.PENDING]
        for step in remaining:
            step.status = StepStatus.SKIPPED
        if failed is not None:
            analysis = self.s.verifier.analyse_failure(failed.result)
            if analysis.ask_user:
                task.status = TaskStatus.WAITING_USER
                failed.step.decision_reason += f" | {analysis.ask_user}"
            else:
                task.status = TaskStatus.FAILED
            task.error = failed.step.error
        else:
            task.status = TaskStatus.FAILED
        task.answer = await self._compose_answer(task, plan, plan.steps, state.sources, None)
        task.steps = step_records(plan.steps)
        task.result = {"plan": plan.model_dump(mode="json"), "sources": state.sources}
        await self._persist(task)
        await self.s.bus.emit(
            EventType.FAILED, message=f"Task failed: {task.error}", task_id=task.id
        )
        await self.s.bus.set_dot_status(DotStatus.ERROR, task.error or "failed")

    # ------------------------------------------------------------------ narration

    @staticmethod
    def _untrusted_from_state(state: DobotGraphState) -> str:
        """Untrusted content (screen OCR + FACT memories) for the JEV signal bundle."""
        bundle: ContextBundle | None = state.get("bundle")
        if bundle is None:
            return ""
        return "\n".join(
            filter(
                None,
                [
                    bundle.screen.ocr_text if bundle.screen else "",
                    *[hit.memory.content for hit in bundle.memories if hit.memory.type is MemoryType.FACT],
                ],
            )
        )

    async def _shadow_stage(self, task: TaskRecord, state: DobotGraphState) -> None:
        """Shadow mode: expand skills, build previews, show the plan — execute nothing."""
        s = self.s
        mode = state["mode"]
        plan: Plan = state["plan"]
        resumable = ResumableState(
            plan=plan, decisions=state.get("decisions") or {}, message=state["message"], shadow=True, mode=mode
        )
        resumable.approval_pending = None
        # Expand skill invocations first, so the preview names the real actions (file counts etc.)
        # rather than "run skill X".
        expanded = await self._expand_skills_for_preview(plan, state["message"], task.id)
        resumable.plan = expanded
        decisions = await s.decision.evaluate_plan(
            expanded,
            ctx=self._policy_context(state["message"], mode),
            signals=JEVSignals(
                untrusted_content=self._untrusted_from_state(state),
                user_message=state["message"],
            ),
            task_id=task.id,
        )
        resumable.decisions = decisions.decisions
        state["plan"] = expanded
        state["decisions"] = decisions.decisions
        previews = await self._preview_plan(expanded, decisions, task.id)
        task.status = TaskStatus.WAITING_USER
        task.result = {
            "plan": expanded.model_dump(mode="json"),
            "shadow": True,
            "preview": previews,
            "state": resumable.model_dump(mode="json"),
        }
        task.answer = self._shadow_answer(expanded, decisions, previews, mode)
        task.steps = step_records(expanded.steps)
        await self._persist(task)
        await s.journal.append(
            task.id,
            "shadow_plan",
            expanded.summary or expanded.intent,
            mode=mode,
            steps=len(expanded.steps),
            tools=[step.action.tool for step in expanded.steps],
        )
        await s.bus.emit(
            EventType.SHADOW_PLAN,
            message="Shadow mode: plan prepared, nothing executed",
            task_id=task.id,
            preview=previews,
        )
        await s.bus.set_dot_status(DotStatus.APPROVAL_REQUIRED, "shadow plan ready")

    async def _refuse_stage(self, task: TaskRecord, state: DobotGraphState) -> None:
        """The plan was refused before execution."""
        s = self.s
        plan: Plan = state["plan"]
        blocked = state.get("blocked_steps") or []
        decisions_map = state.get("decisions") or {}
        decisions = SimpleNamespace(decision_for=lambda step_id: decisions_map.get(step_id))
        await s.journal.append(
            task.id, "blocked", "the plan was refused before execution", steps=blocked
        )
        task.status = TaskStatus.FAILED
        task.error = "blocked by policy"
        task.answer = self._blocked_answer(plan, decisions, blocked)
        task.result = {
            "plan": plan.model_dump(mode="json"),
            "blocked_steps": blocked,
        }
        task.steps = step_records(plan.steps)
        await self._persist(task)
        await s.bus.emit(
            EventType.FAILED,
            message="Refused: " + (task.answer.splitlines()[0][:160] if task.answer else "blocked"),
            task_id=task.id,
            blocked_steps=blocked,
        )
        await s.bus.set_dot_status(DotStatus.ERROR, "refused a blocked action")
        await self._remember_episode(task, state["message"], plan, None, blocked=True)

    def _blocked_answer(self, plan: Plan, decisions, blocked_steps: list[str]) -> str:
        lines = ["I stopped before doing anything, because a safety policy forbids this action."]
        for step in plan.steps:
            if step.id not in blocked_steps:
                continue
            decision = decisions.decision_for(step.id)
            lines.append(f"• {step.action.description or step.action.tool}: {step.error or decision and decision.blocked_reason}")
            if decision and decision.reasons:
                lines.append(f"  reason: {decision.reasons[-1][:300]}")
        lines.append("Nothing was changed on your computer.")
        return "\n".join(lines)

    def _shadow_answer(self, plan: Plan, decisions, previews: dict[str, list[str]], mode: str = "agent") -> str:
        asking = mode == "ask"
        lines = [
            ("ASK MODE — " if asking else "SHADOW MODE — ")
            + "here is what I would do. Nothing has been executed.",
            "",
        ]
        for step in plan.steps:
            decision = decisions.decision_for(step.id)
            badge = {"ALLOW": "✓", "APPROVAL": "!", "BLOCK": "×"}.get(
                decision.verdict.value if decision else "ALLOW", "•"
            )
            risk = decision.risk.value if decision else "LOW"
            lines.append(f"{badge} [{risk}] {step.action.description or step.action.tool}")
            for line in previews.get(step.id, [])[:6]:
                lines.append(f"    {line}")
        lines.append("")
        lines.append(
            "Switch to Assist or Agent when you want me to carry this out."
            if asking
            else "Say 'execute the plan' to run it, or adjust it first."
        )
        return "\n".join(lines)

    async def _preview_plan(self, plan: Plan, decisions, task_id: str) -> dict[str, list[str]]:
        ctx = self._tool_context(task_id, None)
        previews: dict[str, list[str]] = {}
        for step in plan.steps:
            if step.action.tool in {"fs_delete", "fs_move", "message_send", "automation_create"}:
                previews[step.id] = await self._preview_step(step, ctx)
        return previews

    async def _preview_step(self, step: PlanStep, ctx: ToolContext) -> list[str]:
        tool = self.s.registry.get(step.action.tool)
        if tool is None:
            return []
        try:
            return await tool.preview(step.action.params, ctx)
        except Exception as exc:  # noqa: BLE001 - previews are best-effort
            logger.info("preview failed for %s (%s)", step.action.tool, exc)
            return []

    def _collect_sources(self, run: StepRun, state: ResumableState) -> None:
        output = run.result.output
        if isinstance(output, dict):
            sources = output.get("sources")
            if isinstance(sources, list):
                for source in sources:
                    if isinstance(source, dict) and source.get("url"):
                        state.sources.append(source)
        seen: set[str] = set()
        unique: list[dict] = []
        for source in state.sources:
            url = str(source.get("url", ""))
            if url and url not in seen:
                seen.add(url)
                unique.append(source)
        state.sources = unique[:12]

    async def _replan_from_skill(
        self, task_id: str, run: StepRun, state: ResumableState
    ) -> Plan | None:
        output = run.result.output if isinstance(run.result.output, dict) else {}
        instruction = (
            f"The user invoked the saved skill '{output.get('skill')}'. Follow this workflow:\n"
            f"{output.get('instruction', '')}\n"
            f"Workflow steps: {json.dumps(output.get('workflow') or [], ensure_ascii=False)[:1200]}\n"
            f"Inputs: {json.dumps(output.get('inputs') or {}, ensure_ascii=False)[:400]}\n"
            "Return JSON with the concrete steps to execute now."
        )
        bundle = await self.s.context.build(
            self.s.context_request_factory(message=state.message), task_id=task_id
        )
        return await self.s.planner.plan(
            state.message, bundle, tier=self.s.router.route(state.message).tier,
            task_id=task_id, extra_instruction=instruction,
        )

    async def _expand_skills_for_preview(self, plan: Plan, message: str, task_id: str) -> Plan:
        """Turn ``skill_run`` steps into the skill's concrete steps (used by shadow mode)."""
        expanded: list[PlanStep] = []
        for step in plan.steps:
            if step.action.tool != "skill_run":
                expanded.append(step)
                continue
            skill = self.s.skills.get(str(step.action.params.get("name", "")))
            if skill is None:
                expanded.append(step)
                continue
            output = {
                "skill": skill.name,
                "instruction": skill.body,
                "workflow": skill.workflow,
                "inputs": step.action.params.get("inputs") or {},
                "replan": True,
            }
            fake = StepRun(step=step, result=ExecutionResult(ok=True, tool="skill_run", output=output))
            nested = await self._replan_from_skill(
                task_id, fake, ResumableState(plan=plan, message=message)
            )
            if nested and nested.steps:
                expanded.extend(nested.steps)
            else:
                expanded.append(step)
        for index, step in enumerate(expanded):
            step.index = index
        plan.steps = expanded
        return plan

    async def _compose_answer(
        self,
        task: TaskRecord,
        plan: Plan,
        steps: list[PlanStep],
        sources: list[dict],
        verification: Any,
    ) -> str:
        executed = [step for step in steps if step.status in {StepStatus.COMPLETED, StepStatus.FAILED}]
        if not steps and plan.answer:
            return plan.answer

        facts: list[str] = []
        for step in executed:
            output = (step.result or {}).get("output")
            summary = self._summarise_output(step, output)
            facts.append(f"- {step.action.tool}: {summary}")
        if verification is not None:
            facts.append(f"- verification: {verification.summary_line()}")
        if sources:
            facts.append(f"- sources: {len(sources)}")

        research_summary = next(
            (
                str((step.result or {}).get("output", {}).get("summary"))
                for step in executed
                if step.action.tool in {"tavily_research", "research"}
                and isinstance((step.result or {}).get("output"), dict)
                and (step.result or {}).get("output", {}).get("summary")
            ),
            "",
        )

        reasoner = self.s.reasoner
        if reasoner.available:
            # Research findings are the one place raw scraped text reaches the model, so they are the
            # one place a structural trim actually pays for itself.
            findings = research_summary
            juice = getattr(self.s, "tokenjuice", None)
            if findings and juice is not None:
                findings = juice.compress(
                    "research-findings", findings, max_chars=2400, task_id=task.id
                ).text
            prompt = (
                f"The user asked: {plan.intent or task.title}\n\n"
                "What actually happened:\n" + "\n".join(facts) + "\n\n"
                + (f"Research findings:\n{findings}\n\n" if findings else "")
                + (f"Draft answer from planning: {plan.answer}\n\n" if plan.answer else "")
                + "Reply to the user in at most 120 words. Be concrete about what was done and what "
                "was verified. If something failed or could not be verified, say so plainly. "
                "Do not claim anything that is not in the facts above."
            )
            with cost_scope(task_id=task.id, purpose="answer"):
                response = await reasoner.text(
                    prompt,
                    system="You are Dobot, a desktop agent reporting back to its user. Be brief and honest.",
                    max_tokens=400,
                    # The answer is a summary of facts already gathered; a thinking trace here is
                    # pure latency on the most user-visible step of the whole loop.
                    thinking=False,
                )
            if not response.degraded and response.content.strip():
                return response.content.strip()

        if plan.answer and not executed:
            return plan.answer
        if research_summary:
            return research_summary
        if not executed:
            return plan.summary or "Nothing needed to be done."
        lines = ["Here is what I did:"]
        lines.extend(facts)
        if sources:
            lines.append("")
            lines.extend(
                f"[{index}] {source.get('title', '')} — {source.get('url', '')}"
                for index, source in enumerate(sources[:6], start=1)
            )
        return "\n".join(lines)

    @staticmethod
    def _summarise_output(step: PlanStep, output: Any) -> str:
        if not isinstance(output, dict):
            return str(output)[:160] if output is not None else "no output"
        if "moved" in output:
            return f"moved {output.get('count', len(output.get('moved', [])))} file(s) to {output.get('moved', [''])[-1] if output.get('moved') else 'destination'}"
        if "deleted" in output:
            return f"deleted {output.get('count', 0)} file(s)"
        if "path" in output and "sha256" in output:
            return f"wrote {output.get('bytes', 0)} bytes to {output['path']}"
        if "summary" in output and output.get("summary"):
            return str(output["summary"])[:200]
        if "sources" in output:
            return f"found {len(output.get('sources') or [])} sources"
        if "content" in output:
            return f"read {len(str(output['content']))} characters"
        if "count" in output:
            return f"{output['count']} entries"
        if "memory_id" in output:
            return f"remembered: {str(output.get('content'))[:80]}"
        if "next_run_at" in output:
            return f"scheduled for {output.get('next_run_at')}"
        if "sent" in output:
            return "drafted (not sent: no provider configured)"
        if "text" in output:
            return f"read {len(str(output.get('text', '')))} characters from screen"
        if "exit_code" in output:
            return f"exit code {output['exit_code']}"
        return str(output)[:160]

    async def _remember_episode(
        self,
        task: TaskRecord,
        message: str,
        plan: Plan,
        verification: Any,
        *,
        blocked: bool = False,
    ) -> None:
        if not self.s.settings.dobot_env == "development" and blocked is False and task.source.startswith("automation"):
            pass  # automations still get episodes; kept explicit for readability
        content = f"Task '{task.title[:80]}' → {task.status.value.lower()}. Request: {message[:200]}"
        if verification is not None:
            content += f" Verification: {verification.summary_line()}."
        if blocked:
            content += " Blocked by policy before execution."
        try:
            await self.s.memory.remember(
                content,
                type=MemoryType.EPISODE,
                importance=0.3,
                tags=[task.status.value.lower()],
                source="orchestrator",
            )
        except Exception as exc:  # noqa: BLE001 - memory must never break a task
            logger.warning("episode memory failed (%s)", exc)

    # ------------------------------------------------------------------ response

    async def _response(self, task: TaskRecord) -> ChatResponse:
        doc = await self.s.store.get("tasks", task.id)
        if doc:
            task = TaskRecord.model_validate(doc)
        plan_dump = (task.result or {}).get("plan")
        plan = Plan.model_validate(plan_dump) if plan_dump else None
        state_dump = (task.result or {}).get("state") or {}
        decisions = [
            Decision.model_validate(value) for value in (state_dump.get("decisions") or {}).values()
        ]
        pending = task.status is TaskStatus.WAITING_APPROVAL
        approvals = await self.s.approvals.pending() if pending else []
        approvals = [item for item in approvals if item.task_id == task.id]
        verifications: list[VerificationResult] = []
        for step in plan.steps if plan else []:
            if step.verification:
                verifications.append(VerificationResult.model_validate(step.verification))

        # Timeline comes straight from the bus for this task, so it is never a race with the async
        # activity logger; the persisted log is used as a fallback (e.g. after a restart).
        events = self.s.bus.history(limit=400, task_id=task.id)
        timeline = [
            ActivityRecord(
                task_id=event.task_id,
                event_type=event.type.value,
                message=event.message[:500],
                metadata={key: value for key, value in event.data.items() if key != "plan"},
                timestamp=event.timestamp,
            )
            for event in events
        ]
        if not timeline:
            timeline = await self.s.activity.for_task(task.id, limit=200)
        providers = await self.s.provider_status()
        return ChatResponse(
            task_id=task.id,
            status=task.status,
            answer=task.answer,
            plan=plan,
            decisions=decisions,
            sources=[source for source in (task.sources or [])],
            verifications=verifications,
            approvals=approvals,
            timeline=timeline,
            error=task.error,
            providers=providers,
        )
