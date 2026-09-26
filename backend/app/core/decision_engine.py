"""The decision engine: Nemotron says what it wants, this decides whether it may.

Order of authority:

1. Deterministic policies — a BLOCK here is final.
2. Risk classification — sets the floor.
3. JEV — advisory; may escalate, never de-escalate.
4. Hard invariants — CRITICAL actions are never auto-approved, shadow mode never executes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, get_settings
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.schemas import ActionSpec, Decision, Plan, PlanStep, RiskLevel, Verdict
from app.security.jev import JEVEngine, JEVSignals, build_jev_engine
from app.security.policies import PolicyContext, PolicyEngine
from app.security.risk import classify, requires_approval

logger = get_logger(__name__)

_VERDICT_RANK = {Verdict.ALLOW: 0, Verdict.APPROVAL: 1, Verdict.BLOCK: 2}

#: The execution mode *is* the approval threshold. Above it, a human decides.
#:
#: `agent` is the specification default — LOW and MEDIUM proceed, HIGH and CRITICAL are gated.
#: `assist` confirms MEDIUM as well, so nothing beyond a read happens unattended.
#: `ask` never executes at all — the orchestrator turns it into a plan-only run — so its threshold is
#: deliberately the same as `agent`: the preview should show what the plan *would* do, not a wall of
#: "approval required" for actions that are moot because nothing will run.
MODE_APPROVAL_THRESHOLD: dict[str, RiskLevel] = {
    "ask": RiskLevel.HIGH,
    "assist": RiskLevel.MEDIUM,
    "agent": RiskLevel.HIGH,
}

EXECUTION_MODES: tuple[str, ...] = ("ask", "assist", "agent")


@dataclass
class PlanDecision:
    verdict: Verdict
    overall_risk: RiskLevel
    decisions: dict[str, Decision] = field(default_factory=dict)
    blocked_steps: list[str] = field(default_factory=list)
    approval_steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        return self.verdict is not Verdict.BLOCK

    def decision_for(self, step_id: str) -> Decision | None:
        return self.decisions.get(step_id)


class DecisionEngine:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        policy_engine: PolicyEngine | None = None,
        jev: JEVEngine | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.policies = policy_engine or PolicyEngine()
        self.jev = jev or build_jev_engine(self.settings)
        self.bus = bus or get_event_bus()

    # ------------------------------------------------------------------ context

    def approval_threshold(self, mode: str | None) -> RiskLevel:
        """Highest risk that may still run unattended in this mode."""
        return MODE_APPROVAL_THRESHOLD.get(mode or "", RiskLevel.HIGH)

    def policy_context(
        self,
        *,
        autonomy: str = "assisted",
        user_message: str = "",
        mode: str | None = None,
        blocked_skills: list[str] | None = None,
    ) -> PolicyContext:
        return PolicyContext(
            allowed_paths=list(self.settings.allowed_paths) or [Path.home().resolve()],
            allowed_hosts=self.settings.allowed_hosts,
            sandbox_provider=self.settings.sandbox_provider,
            shadow_mode=self.settings.shadow_mode,
            user_message=user_message,
            autonomy=autonomy,
            require_write_approval=self.settings.require_write_approval,
            mode=mode or self.settings.execution_mode,
            protected_paths=list(self.settings.protected_paths_resolved),
            blocked_skills=list(blocked_skills or []),
        )

    # ------------------------------------------------------------------ evaluation

    async def evaluate_step(
        self,
        step: PlanStep,
        ctx: PolicyContext,
        signals: JEVSignals,
    ) -> Decision:
        action: ActionSpec = step.action
        risk, risk_reasons = classify(action)
        outcomes = self.policies.evaluate(action, ctx)
        verdict = self.policies.combine(outcomes)
        reasons = list(risk_reasons)
        policy_names: list[str] = []
        blocked_reason = ""

        for outcome in outcomes:
            policy_names.append(outcome.policy)
            if outcome.verdict is not Verdict.ALLOW:
                reasons.append(f"{outcome.policy}: {outcome.reason}")
            if outcome.escalate_to and outcome.escalate_to.rank > risk.rank:
                risk = outcome.escalate_to
            if outcome.verdict is Verdict.BLOCK and not blocked_reason:
                blocked_reason = outcome.reason

        jev_notes: list[str] = []
        if verdict is not Verdict.BLOCK and self.jev.enabled:
            advice = await self.jev.advise(action, signals, base_risk=risk)
            jev_notes = advice.notes
            if advice.escalate_to and advice.escalate_to.rank > risk.rank:
                risk = advice.escalate_to
                judge_label = "Laya" if advice.judge == "laya" else "JEV heuristics"
                reasons.append(
                    f"{judge_label} escalated to {risk.value} (score {advice.score}): "
                    f"{', '.join(advice.notes)}"
                )
            if advice.injection_suspected and verdict is Verdict.ALLOW:
                verdict = Verdict.APPROVAL
                reasons.append("possible prompt injection in untrusted content — confirming with user")

        # Hard invariant: CRITICAL is always human-gated.
        if risk is RiskLevel.CRITICAL and verdict is Verdict.ALLOW:
            verdict = Verdict.APPROVAL
            reasons.append("CRITICAL actions are never executed automatically")

        # The mode sets the approval threshold. Policies and JEV above can only add to it — nothing
        # here can lower it, and HIGH/CRITICAL stay gated in every mode.
        threshold = self.approval_threshold(ctx.mode)
        if verdict is Verdict.ALLOW and (risk.rank >= threshold.rank or requires_approval(risk)):
            verdict = Verdict.APPROVAL
            reasons.append(f"{risk.value} actions require approval ({ctx.mode} mode)")

        if verdict is Verdict.BLOCK and ctx.shadow_mode is False and ctx.autonomy == "autonomous":
            block_notes = [reason for reason in reasons if ": " in reason]
            blocked_reason = blocked_reason or (block_notes[-1] if block_notes else "blocked by policy")

        return Decision(
            verdict=verdict,
            risk=risk,
            step_id=step.id,
            reasons=reasons,
            policies=policy_names,
            jev_notes=jev_notes,
            requires_approval=verdict is Verdict.APPROVAL,
            blocked_reason=blocked_reason,
        )

    async def evaluate_plan(
        self,
        plan: Plan,
        *,
        ctx: PolicyContext | None = None,
        signals: JEVSignals | None = None,
        task_id: str = "",
    ) -> PlanDecision:
        ctx = ctx or self.policy_context()
        signals = signals or JEVSignals()
        overall = Verdict.ALLOW
        highest_risk = RiskLevel.LOW
        decisions: dict[str, Decision] = {}
        blocked: list[str] = []
        approvals: list[str] = []

        for step in plan.steps:
            decision = await self.evaluate_step(step, ctx, signals)
            decisions[step.id] = decision
            step.risk = decision.risk
            step.verdict = decision.verdict
            step.decision_reason = "; ".join(decision.reasons[:3])
            if decision.risk.rank > highest_risk.rank:
                highest_risk = decision.risk
            if decision.verdict is Verdict.BLOCK:
                blocked.append(step.id)
            elif decision.verdict is Verdict.APPROVAL:
                approvals.append(step.id)
            if _VERDICT_RANK[decision.verdict] > _VERDICT_RANK[overall]:
                overall = decision.verdict
            await self.bus.emit(
                EventType.DECISION,
                message=f"{action_label(step.action)} → {decision.verdict.value} ({decision.risk.value})",
                task_id=task_id,
                step_id=step.id,
                verdict=decision.verdict.value,
                risk=decision.risk.value,
                reasons=decision.reasons,
            )

        return PlanDecision(
            verdict=overall,
            overall_risk=highest_risk,
            decisions=decisions,
            blocked_steps=blocked,
            approval_steps=approvals,
        )


def action_label(action: ActionSpec) -> str:
    target = (
        action.params.get("path")
        or action.params.get("url")
        or action.params.get("query")
        or action.params.get("command")
        or ""
    )
    label = action.tool if not target else f"{action.tool}({str(target)[:48]})"
    return label
