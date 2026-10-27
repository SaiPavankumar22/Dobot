"""Service composition.

One place that constructs the runtime graph and owns its lifecycle, so the API layer stays thin and
tests can build an isolated, fully-wired system against a temporary state directory.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from app import __version__
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
from app.core.process_supervisor import ProcessSupervisor
from app.core.router import ModelRouter
from app.core.scheduler import Scheduler
from app.core.sessions import SessionService
from app.core.tokenjuice import TokenJuice, build_tokenjuice
from app.core.trace_store import TraceStore
from app.core.verifier import Verifier
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.memory.canonical import CanonicalLedger, build_canonical_ledger
from app.memory.manager import build_memory_manager
from app.memory.store import build_record_store
from app.schemas import Region, TaskStatus
from app.security.approvals import ApprovalStore
from app.security.grants import GrantStore
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
    traces: TraceStore
    process_supervisor: ProcessSupervisor
    canonical: CanonicalLedger
    identity: IdentityLayer
    interceptors: Interceptors
    consolidator: Consolidator | None
    voice: Voice
    #: Remembered permissions (allow once / always allow) shared by decision engine and sandbox.
    grants: GrantStore
    #: Durable chat transcripts, the bounded message buffer and the rolling session summary.
    sessions: SessionService
    #: Multi-user only (MULTI_USER=true): accounts, tokens, invites. None in single-user mode.
    user_manager: Any = None
    orchestrator: Any = None
    extras: dict[str, Any] = field(default_factory=dict)
    _started: bool = False
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)

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
        conversation_id: str = "",
    ) -> ContextRequestData:
        return ContextRequestData(
            message=message,
            region=region,
            image_base64=image_base64,
            question=question or message,
            application=application,
            window_title=window_title,
            attachments=attachments or [],
            conversation_id=conversation_id,
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
            process_supervisor=self.process_supervisor,
        )

    async def provider_status(self) -> dict[str, str]:
        """What the UI shows: connection status, never credentials.

        A configured key is not the same as a working endpoint, so Nemotron reports ``unreachable``
        when the endpoint cannot be reached — otherwise the UI would claim "connected" while every
        request degrades to the offline path.
        """
        if not self.reasoner.available:
            nemotron = "not_configured"
        elif await self.reasoner.health():
            nemotron = "connected"
        else:
            nemotron = "key_rejected" if getattr(self.reasoner, "key_rejected", False) else "unreachable"
        tavily = "connected" if getattr(self.tavily, "available", False) else "not_configured"
        # A saved key is not a working key, but the decide endpoint bills per call — the Doctor
        # does the live probe; here the UI only needs configured-vs-not.
        autotrust = "connected" if self.settings.has_novita else "not_configured"
        runtime_info = await self.runtime.info()
        sandbox = await self.sandbox.status()
        return {
            "nemotron": nemotron,
            "reasoning_provider": self.settings.reasoning_provider,
            "tavily": tavily,
            "autotrust": autotrust,
            "memory_store": getattr(self.store, "name", "unknown"),
            "vectors": getattr(self.memory.vectors, "name", "unknown"),
            "embedder": getattr(self.memory.embedder, "name", "unknown"),
            "agent_runtime": f"{runtime_info.name}:{'available' if runtime_info.available else 'unavailable'}",
            "sandbox": f"{sandbox.provider}:{sandbox.isolation}",
            "shadow_mode": "on" if self.settings.shadow_mode else "off",
            "screen_capture": self.settings.screen_capture,
        }

    async def capabilities(self) -> dict[str, Any]:
        """Describe what this backend can actually do in its current environment."""
        from app.tools.browser_drive import get_drive

        runtime_info = await self.runtime.info()
        sandbox = await self.sandbox.status()
        browser = await get_drive().capability_status()
        screen_available = bool(self.settings.screen_capture != "off")
        file_roots = [str(path) for path in self.settings.allowed_paths if path.exists()]
        location = (os.environ.get("DOBOT_BACKEND_LOCATION") or "").strip().lower()
        if location not in {"local", "hosted", "remote"}:
            location = "local" if self.settings.dobot_host in {"127.0.0.1", "localhost", "::1"} else "remote"
        is_local = location == "local"
        if not screen_available:
            screen_detail = "Screen capture is disabled by configuration"
        elif is_local:
            screen_detail = "The desktop client can submit explicit screen captures"
        else:
            screen_detail = "This backend is remote; it cannot capture this laptop's screen"
        if not file_roots:
            files_detail = "No allowed file roots are available"
        elif is_local:
            files_detail = "File tools operate on this laptop"
        else:
            files_detail = "File tools operate on the remote backend"
        return {
            "contract_version": "1",
            "backend_location": location,
            "browser": browser,
            "hermes": {
                "available": bool(runtime_info.available),
                "mode": runtime_info.mode,
                "name": runtime_info.name,
                "detail": runtime_info.detail,
            },
            "local_screen": {
                "available": is_local and screen_available,
                "mode": self.settings.screen_capture,
                "detail": screen_detail,
            },
            "local_files": {
                "available": is_local and bool(file_roots),
                "roots": file_roots,
                "detail": files_detail,
            },
            "voice": {
                "available": bool(self.voice.available),
                "enabled": bool(self.settings.voice_enabled),
                "detail": self.voice.plan.detail,
            },
            "sandbox": {
                "provider": sandbox.provider,
                "isolation": sandbox.isolation,
                "degraded": sandbox.degraded,
                "detail": sandbox.details,
            },
        }

    async def runtime_status(self) -> dict[str, Any]:
        """Return lifecycle metadata consumed by the desktop supervisor and Doctor page."""
        location = (os.environ.get("DOBOT_BACKEND_LOCATION") or "").strip().lower()
        if location not in {"local", "hosted", "remote"}:
            location = "local" if self.settings.dobot_host in {"127.0.0.1", "localhost", "::1"} else "remote"
        state = "ready" if self._started else "starting"
        return {
            "contract_version": "1",
            "state": state,
            "backend_location": location,
            "pid": os.getpid(),
            "version": __version__,
            "host": self.settings.dobot_host,
            "port": self.settings.dobot_port,
            "uptime_seconds": round(max(0.0, time.monotonic() - self._started_monotonic), 3),
            "state_dir": str(self.settings.state_dir),
            "data_dir": str(self.settings.data_dir),
            "logs_dir": str(self.settings.logs_dir),
            "supervised_processes": len(self.process_supervisor.handles()),
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
            "data_policy": {
                "remote_sensitive_data": "blocked",
                "remote_secret_data": "blocked",
                "local_only_supported": True,
            },
            "shadow_mode": self.settings.shadow_mode,
            "require_write_approval": self.settings.require_write_approval,
            "execution_mode": self.resolved_mode(),
            "protected_paths": self.settings.protected_path_list,
            "grants": self.grants.summary(),
            "skill_scan": {
                "mode": self.settings.skill_scan_mode,
                "flagged": self.skills.flagged_names(),
                "blocked": self.skills.blocked_names(),
            },
            "jev": {
                "enabled": self.settings.jev_enabled,
                "mode": self.settings.jev_mode,
                "judge": self.settings.jev_model if self.settings.has_novita else "none",
                "screen_check": self.settings.jev_screen_check,
            },
            "kill_switch_hotkey": self.settings.kill_switch_hotkey,
            "active_tasks": self.kill_switch.active_ids,
            "allowed_paths": [str(path) for path in self.settings.allowed_paths],
            "workspace_root": str(self.settings.state_dir / "workspaces"),
            "network_allowlist": self.settings.allowed_hosts or [],
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
        await self._recover_interrupted_tasks()
        self._started = True
        self._started_monotonic = time.monotonic()
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

    async def _recover_interrupted_tasks(self) -> None:
        """Mark work interrupted by a backend restart for explicit user recovery.

        Re-running an arbitrary tool after a crash can duplicate side effects, so recovery is
        deliberately conservative: approval-paused tasks remain resumable, while active tasks are
        surfaced as recovery-required instead of being silently replayed.
        """
        for doc in await self.store.all("tasks", limit=500):
            if doc.get("status") not in {
                TaskStatus.PLANNING.value,
                TaskStatus.IN_PROGRESS.value,
                TaskStatus.VERIFYING.value,
                TaskStatus.RETRYING.value,
            }:
                continue
            doc["status"] = TaskStatus.RECOVERY_REQUIRED.value
            doc["error"] = "backend restarted before this task finished; review before retrying"
            await self.store.insert("tasks", doc)
            await self.traces.append(doc.get("id", ""), "recovery_required", reason=doc["error"])
            await self.bus.emit(
                EventType.RECOVERY_REQUIRED,
                task_id=str(doc.get("id", "")),
                message=doc["error"],
            )

    async def shutdown(self) -> None:
        if self.consolidator is not None:
            await self.consolidator.stop()
        await self.scheduler.stop()
        await self.activity.stop()
        from app.tools.browser_drive import close_drive

        for closer in (
            self.memory.close,
            self.tavily.aclose,
            self.research.aclose,
            self.reasoner.aclose,
            self.browser.aclose,
            self.runtime.close,
            self.sandbox.close,
            self.store.close,
            close_drive,
        ):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.info("shutdown step failed (%s)", exc)


async def build_services(settings: Settings | None = None, *, bus: EventBus | None = None) -> Services:
    settings = settings or get_settings()
    from app.security.local_secrets import hydrate

    hydrate(settings)
    bus = bus or get_event_bus()

    store = await build_record_store(settings)
    # The scheduler always holds the *base* store: a fired job and the startup restore run
    # outside any request, where a scoped wrapper would see nothing. Ownership travels on
    # ``AutomationRecord.user_id`` instead and is enforced at query time (``_visible``).
    base_store = store
    user_manager = None
    if settings.multi_user:
        # Every collection except users/invites is prefixed per requester (see tenancy.py);
        # the scheduler re-enters the owner's scope around each fired automation.
        from app.core.tenancy import UserScopedStore
        from app.core.users import UserManager

        store = UserScopedStore(store)
        user_manager = UserManager(store)
    memory = build_memory_manager(store, bus)
    skills = SkillsLibrary(
        directory=settings.skills_dir,
        user_directory=settings.state_dir / "user_skills",
    )
    skills.load()
    grants = GrantStore(store, bus)
    await grants.load()
    sandbox = build_sandbox(settings, grants=grants)
    runtime = build_runtime(settings)
    reasoner = build_reasoner(settings)
    tavily = TavilyClient(settings)
    browser = BrowserSession()
    research = ResearchAgent(tavily=tavily, reasoner=reasoner, bus=bus, settings=settings)
    registry = default_registry()
    tokenjuice = build_tokenjuice(settings)
    decision = DecisionEngine(settings=settings, bus=bus, grants=grants)
    planner = Planner(
        reasoner=reasoner,
        registry=registry,
        bus=bus,
        settings=settings,
        tokenjuice=tokenjuice,
        runtime=runtime,
    )
    verifier = Verifier(registry, bus)
    approvals = ApprovalStore(store, bus)
    identity = build_identity(settings)
    canonical = build_canonical_ledger(store, settings)
    journal = RunJournal(store, limit=int(getattr(settings, "run_journal_limit", 200) or 200))
    sessions = SessionService(store, settings=settings, reasoner=reasoner)

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
            sessions=sessions,
        ),
        tokenjuice=tokenjuice,
        cost=get_cost_ledger(),
        journal=journal,
        traces=TraceStore(store),
        process_supervisor=ProcessSupervisor(),
        canonical=canonical,
        identity=identity,
        interceptors=build_interceptors(settings),
        grants=grants,
        sessions=sessions,
        consolidator=build_consolidator(
            memory=memory,
            canonical=canonical,
            identity=identity,
            settings=settings,
            journal=journal,
            store=store,
            reasoner=reasoner,
        ),
        voice=build_voice(settings),
        user_manager=user_manager,
    )

    orchestrator = Orchestrator(services)
    services.orchestrator = orchestrator
    services.scheduler = Scheduler(
        store=base_store,
        runner=orchestrator.run_automation,
        bus=bus,
        settings=settings,
    )
    return services
