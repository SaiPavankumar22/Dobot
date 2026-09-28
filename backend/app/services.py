"""Service composition.

One place that constructs the runtime graph and owns its lifecycle, so the API layer stays thin and
tests can build an isolated, fully-wired system against a temporary state directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.hermes import build_runtime
from app.agents.nemotron import build_reasoner
from app.agents.research_agent import ResearchAgent
from app.agents.sandbox import build_sandbox
from app.config import Settings, get_settings
from app.core.activity import ActivityLog
from app.core.consolidation import Consolidator, build_consolidator
from app.core.context_engine import ContextEngine, ContextRequestData
from app.core.cost import CostLedger, RunJournal, get_cost_ledger
from app.core.decision_engine import EXECUTION_MODES, DecisionEngine
from app.core.identity import IdentityLayer, build_identity
from app.core.killswitch import KillSwitch, get_kill_switch
from app.core.orchestrator import Orchestrator
from app.core.planner import Planner
from app.core.router import ModelRouter
from app.core.scheduler import Scheduler
from app.core.tokenjuice import TokenJuice, build_tokenjuice
from app.core.verifier import Verifier
from app.events import EventBus, get_event_bus
from app.logging_setup import get_logger
from app.memory.canonical import CanonicalLedger, build_canonical_ledger
from app.memory.manager import build_memory_manager
from app.memory.store import build_record_store
from app.schemas import Region
from app.security.approvals import ApprovalStore
from app.security.interceptors import Interceptors, build_interceptors
from app.security.policies import MUTATING_TOOLS
from app.security.risk import TOOL_RISK_FLOORS
from app.skills_loader import SkillsLibrary
from app.tools.base import ToolContext
from app.tools.browser import BrowserSession
from app.tools.registry import ToolRegistry, default_registry
from app.tools.tavily import TavilyClient
from app.voice import Voice, build_voice

logger = get_logger(__name__)


@dataclass
class Services:
    settings: Settings
    bus: EventBus
    store: Any
    memory: Any
    approvals: ApprovalStore
    sandbox: Any
    runtime: Any
    reasoner: Any
    tavily: TavilyClient
    browser: BrowserSession
    registry: ToolRegistry
    research: ResearchAgent
    skills: SkillsLibrary
    scheduler: Scheduler
    activity: ActivityLog
    kill_switch: KillSwitch
    decision: DecisionEngine
    router: ModelRouter
    planner: Planner
    verifier: Verifier
    context: ContextEngine
    # --- V3 subsystems -------------------------------------------------------
    tokenjuice: TokenJuice
    cost: CostLedger
    journal: RunJournal
    canonical: CanonicalLedger
    identity: IdentityLayer
    interceptors: Interceptors
    consolidator: Consolidator | None
    voice: Voice
    orchestrator: Any = None
    extras: dict[str, Any] = field(default_factory=dict)
    _started: bool = False

    # ------------------------------------------------------------------ helpers

    def context_request_factory(
        self,
        *,
        message: str,
        region: Region | None = None,
        image_base64: str | None = None,
        question: str = "",
        application: str = "",
        window_title: str = "",
        attachments: list[Any] | None = None,
    ) -> ContextRequestData:
        return ContextRequestData(
            message=message,
            region=region,
            image_base64=image_base64,
            question=question or message,
            application=application,
            window_title=window_title,
            attachments=attachments or [],
        )

    def tool_context(self, *, task_id: str = "", token: Any = None) -> ToolContext:
        return ToolContext(
            settings=self.settings,
            sandbox=self.sandbox,
            bus=self.bus,
            token=token,
            task_id=task_id,
            store=self.store,
            memory=self.memory,
            approvals=self.approvals,
            scheduler=self.scheduler,
            skills=self.skills,
            research=self.research,
            tavily=self.tavily,
            hermes=self.runtime,
            browser=self.browser,
        )

    async def provider_status(self) -> dict[str, str]:
        """What the UI shows: connection status, never credentials.

        A configured key is not the same as a working endpoint, so Nemotron reports ``unreachable``
        when the endpoint cannot be reached — otherwise the UI would claim "connected" while every
        request degrades to the offline path.
        """
        if not self.reasoner.available:
            nemotron = "not_configured"
        else:
            nemotron = "connected" if await self.reasoner.health() else "unreachable"
        tavily = "connected" if getattr(self.tavily, "available", False) else "not_configured"
        runtime_info = await self.runtime.info()
        sandbox = await self.sandbox.status()
        return {
            "nemotron": nemotron,
            "tavily": tavily,
            "memory_store": getattr(self.store, "name", "unknown"),
            "vectors": getattr(self.memory.vectors, "name", "unknown"),
            "embedder": getattr(self.memory.embedder, "name", "unknown"),
            "agent_runtime": f"{runtime_info.name}:{'available' if runtime_info.available else 'unavailable'}",
            "sandbox": f"{sandbox.provider}:{sandbox.isolation}",
            "shadow_mode": "on" if self.settings.shadow_mode else "off",
            "screen_capture": self.settings.screen_capture,
        }

    async def security_status(self) -> dict[str, Any]:
        sandbox = await self.sandbox.status()
        runtime_info = await self.runtime.info()
        return {
            "sandbox": {
                "provider": sandbox.provider,
                "isolation": sandbox.isolation,
                "degraded": sandbox.degraded,
                "details": sandbox.details,
            },
            "agent_runtime": {
                "mode": runtime_info.mode,
                "name": runtime_info.name,
                "available": runtime_info.available,
                "toolsets": runtime_info.toolsets,
                "detail": runtime_info.detail,
            },
            "screen_capture": self.settings.screen_capture,
            "continuous_monitoring": False,
            "shadow_mode": self.settings.shadow_mode,
            "require_write_approval": self.settings.require_write_approval,
            "execution_mode": self.resolved_mode(),
            "protected_paths": self.settings.protected_path_list,
            "skill_scan": {
                "mode": self.settings.skill_scan_mode,
                "flagged": self.skills.flagged_names(),
                "blocked": self.skills.blocked_names(),
            },
            "jev": {"enabled": self.settings.jev_enabled, "mode": self.settings.jev_mode},
            "kill_switch_hotkey": self.settings.kill_switch_hotkey,
            "active_tasks": self.kill_switch.active_ids,
            "allowed_paths": [str(path) for path in self.settings.allowed_paths],
            "allowed_networks": self.settings.allowed_hosts,
            "policies": self.decision.policies.summarise(),
            "require_confirmation_for": (
                ["delete", "send", "purchase", "credential_change", "financial"]
                + (["every change to your files"] if self.settings.require_write_approval else [])
            ),
        }

    # ------------------------------------------------------------------ posture

    def resolved_mode(self) -> str:
        configured = (self.settings.execution_mode or "").strip().lower()
        return configured if configured in EXECUTION_MODES else "agent"

    def permission_matrix(self) -> dict[str, Any]:
        """The access policy, at tool-level granularity, as the running configuration actually
        evaluates it. "Automatic" here means the tool's declared floor would run unattended in the
        active mode — a tool can still be gated or blocked at runtime by a condition (a path outside
        the workspace, a credential argument, an evasion pattern).
        """
        settings = self.settings
        mode = self.resolved_mode()
        threshold = self.decision.approval_threshold(mode)
        strict_writes = settings.require_write_approval
        policies = self.decision.policies.policies

        tools: list[dict[str, Any]] = []
        for tool, floor in sorted(TOOL_RISK_FLOORS.items(), key=lambda item: (item[1].rank, item[0])):
            guards = [
                {"name": policy.name, "verdict": policy.verdict.value, "reason": policy.reason}
                for policy in policies
                if tool in policy.tools
            ]
            automatic = floor.rank < threshold.rank
            if automatic and strict_writes and tool in MUTATING_TOOLS:
                automatic = False
            tools.append(
                {
                    "tool": tool,
                    "risk_floor": floor.value,
                    "automatic": automatic,
                    "can_ask": any(guard["verdict"] == "APPROVAL" for guard in guards),
                    "can_deny": any(guard["verdict"] == "BLOCK" for guard in guards),
                    "guards": [guard["name"] for guard in guards],
                }
            )

        return {
            "mode": mode,
            "approval_threshold": threshold.value,
            "require_write_approval": strict_writes,
            "modes": list(EXECUTION_MODES),
            "protected_paths": settings.protected_path_list,
            "skill_scan_mode": settings.skill_scan_mode,
            "flagged_skills": self.skills.flagged_names(),
            "blocked_skills": self.skills.blocked_names(),
            "tools": tools,
        }

    def skill_scan(self) -> dict[str, Any]:
        return {
            "mode": self.settings.skill_scan_mode,
            "allowlist": self.settings.skill_allowlist,
            "blocked": self.skills.blocked_names(),
            "flagged": self.skills.flagged_names(),
            "reports": [report.as_dict() for report in self.skills.reports()],
        }

    # ------------------------------------------------------------------ v3 subsystems

    async def reload_interceptors(self) -> dict[str, Any]:
        """Recompile the runtime rules from GENOME.md and every skill's workflow.json.

        Called at startup and after any edit that could change the rule set, so what the Doctor page
        reports and what actually runs can never drift apart.
        """
        genome = await self.identity.genome_markdown()
        result = self.interceptors.load(
            genome_markdown=genome,
            skill_rules=self.skills.interceptor_rules(),
        )
        result["summary"] = self.interceptors.summarise()
        return result

    async def usage_status(self) -> dict[str, Any]:
        return {
            "model": self.cost.summary(),
            "tokenjuice": self.tokenjuice.summary(),
            "pricing_configured": self.settings.pricing_configured,
        }

    # ------------------------------------------------------------------ lifecycle

    async def startup(self) -> None:
        if self._started:
            return
        await self.activity.start()
        await self.scheduler.start()
        # Identity first: the interceptor rules live inside GENOME.md, so the documents have to exist
        # (and be seeded) before the rules can be compiled or the Doctor can reconcile them.
        await self.identity.ensure()
        await self.reload_interceptors()
        if self.consolidator is not None:
            await self.consolidator.start()
        self._started = True
        logger.info(
            "dobot ready: reasoning=%s research=%s store=%s vectors=%s runtime=%s sandbox=%s "
            "interceptors=%s voice=%s",
            "nemotron" if self.reasoner.available else "offline",
            "tavily" if getattr(self.tavily, "available", False) else "offline",
            getattr(self.store, "name", "?"),
            getattr(self.memory.vectors, "name", "?"),
            (await self.runtime.info()).name,
            (await self.sandbox.status()).provider,
            len(self.interceptors.rules),
            self.voice.plan.engine or "none",
        )

    async def shutdown(self) -> None:
        if self.consolidator is not None:
            await self.consolidator.stop()
        await self.scheduler.stop()
        await self.activity.stop()
        for closer in (
            self.memory.close,
            self.tavily.aclose,
            self.research.aclose,
            self.reasoner.aclose,
            self.browser.aclose,
            self.runtime.close,
            self.sandbox.close,
            self.store.close,
        ):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.info("shutdown step failed (%s)", exc)


async def build_services(settings: Settings | None = None, *, bus: EventBus | None = None) -> Services:
    settings = settings or get_settings()
    bus = bus or get_event_bus()

    store = await build_record_store(settings)
    memory = build_memory_manager(store, bus)
    skills = SkillsLibrary(directory=settings.skills_dir)
    skills.load()
    sandbox = build_sandbox(settings)
    runtime = build_runtime(settings)
    reasoner = build_reasoner(settings)
    tavily = TavilyClient(settings)
    browser = BrowserSession()
    research = ResearchAgent(tavily=tavily, reasoner=reasoner, bus=bus, settings=settings)
    registry = default_registry()
    tokenjuice = build_tokenjuice(settings)
    decision = DecisionEngine(settings=settings, bus=bus)
    planner = Planner(
        reasoner=reasoner,
        registry=registry,
        bus=bus,
        settings=settings,
        tokenjuice=tokenjuice,
    )
    verifier = Verifier(registry, bus)
    approvals = ApprovalStore(store, bus)
    identity = build_identity(settings)
    canonical = build_canonical_ledger(store, settings)
    journal = RunJournal(store, limit=int(getattr(settings, "run_journal_limit", 200) or 200))

    services = Services(
        settings=settings,
        bus=bus,
        store=store,
        memory=memory,
        approvals=approvals,
        sandbox=sandbox,
        runtime=runtime,
        reasoner=reasoner,
        tavily=tavily,
        browser=browser,
        registry=registry,
        research=research,
        skills=skills,
        scheduler=None,  # type: ignore[arg-type]  # set below (needs the orchestrator runner)
        activity=ActivityLog(store, bus),
        kill_switch=get_kill_switch(),
        decision=decision,
        router=ModelRouter(),
        planner=planner,
        verifier=verifier,
        context=ContextEngine(
            memory=memory,
            store=store,
            skills=skills,
            sandbox=sandbox,
            bus=bus,
            settings=settings,
            runtime=runtime,
            identity=identity,
            canonical=canonical,
        ),
        tokenjuice=tokenjuice,
        cost=get_cost_ledger(),
        journal=journal,
        canonical=canonical,
        identity=identity,
        interceptors=build_interceptors(settings),
        consolidator=build_consolidator(
            memory=memory,
            canonical=canonical,
            identity=identity,
            settings=settings,
            journal=journal,
        ),
        voice=build_voice(settings),
    )

    orchestrator = Orchestrator(services)
    services.orchestrator = orchestrator
    services.scheduler = Scheduler(
        store=store,
        runner=orchestrator.run_automation,
        bus=bus,
        settings=settings,
    )
    return services
