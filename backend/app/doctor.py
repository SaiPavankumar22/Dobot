"""Doctor - the honest state of every capability, and the exact command that fixes it.

Health endpoints usually answer one question ("is the process up?"). That is the least interesting
question about a system like this, because almost everything optional degrades silently: a missing key
turns reasoning into an offline stub, a missing binary turns desktop control into a refusal, an
unreadable rule file disables a guard you believe is active.

The Doctor reports each capability as one of five states — ``live``, ``broken``, ``declined``,
``stale``, ``not_configured`` — and pairs every non-live state with the *specific* fix. It also does
something a config dump cannot: it **reconciles intent against reality**, comparing what is declared
(skills on disk, axioms written in GENOME.md, a sandbox provider named in settings) against what is
actually loaded and enforced. A guard that is configured but not applied is the most dangerous state
there is, and it looks identical to a working one from the outside.

Nothing here changes state. It reads, compares, and tells you the truth.
"""

from __future__ import annotations

import importlib.util
import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)

#: Ordered worst-first so the UI can sort meaningfully.
SEVERITY = {"broken": 0, "stale": 1, "not_configured": 2, "declined": 3, "live": 4}
OPTIONAL_STATES = {"not_configured", "declined"}


@dataclass
class Capability:
    id: str
    label: str
    state: str = "not_configured"
    detail: str = ""
    fix: str = ""
    env: str = ""
    optional: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "fix": self.fix,
            "env": self.env,
            "optional": self.optional,
            "meta": self.meta,
        }


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _has_binary(name: str) -> bool:
    return bool(shutil.which(name))


class Doctor:
    """Probes the live service graph. Never raises: a failed probe is itself a finding."""

    def __init__(self, services: Any) -> None:
        self.s = services
        self.settings = services.settings

    # ------------------------------------------------------------------ helpers

    async def _check(self, coro: Any, fallback: Any) -> Any:
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001 - a probe that throws is data, not a crash
            logger.info("doctor probe failed (%s)", exc)
            return fallback

    async def report(self) -> dict[str, Any]:
        capabilities: list[Capability] = []
        for probe in (
            self._reasoning,
            self._research,
            self._embeddings,
            self._record_store,
            self._vector_index,
            self._agent_runtime,
            self._sandbox,
            self._screen,
            self._ocr,
            self._voice,
            self._identity,
            self._axiom_reconciliation,
            self._skill_reconciliation,
            self._interceptors,
            self._skill_scanner,
            self._consolidation,
            self._ledger,
            self._budgets,
            self._api_auth,
            self._kill_switch,
            self._laya,
            self._harness,
            self._langsmith,
            self._deep_research,
            self._transcriber,
        ):
            try:
                capabilities.extend(await probe())
            except Exception as exc:  # noqa: BLE001
                logger.warning("doctor capability probe %s failed (%s)", getattr(probe, "__name__", "?"), exc)

        capabilities.sort(key=lambda item: (SEVERITY.get(item.state, 2), item.id))
        counts: dict[str, int] = {}
        for capability in capabilities:
            counts[capability.state] = counts.get(capability.state, 0) + 1
        blocking = [
            capability
            for capability in capabilities
            if capability.state == "broken" and not capability.optional
        ]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "overall": "degraded" if blocking or counts.get("broken") else "healthy",
            "counts": counts,
            "capabilities": [capability.as_dict() for capability in capabilities],
            "fixes": [
                {
                    "id": capability.id,
                    "label": capability.label,
                    "state": capability.state,
                    "fix": capability.fix,
                    "env": capability.env,
                }
                for capability in capabilities
                if capability.fix and capability.state != "live"
            ],
            "environment": {
                "platform": f"{platform.system()} {platform.release()}",
                "python": sys.version.split()[0],
                "state_dir": str(self.settings.state_dir),
            },
        }

    # ------------------------------------------------------------------ reasoning

    async def _reasoning(self) -> list[Capability]:
        settings = self.settings
        reasoner = self.s.reasoner
        if not getattr(reasoner, "available", False):
            return [
                Capability(
                    id="reasoning",
                    label="Reasoning (Nemotron 3 Ultra via Nebius)",
                    state="not_configured",
                    detail="No API key, so every request degrades to the deterministic offline planner.",
                    fix="Set NEBIUS_API_KEY in .env, then restart the backend.",
                    env="NEBIUS_API_KEY",
                    optional=False,
                    meta={"model": settings.nemotron_model, "base_url": settings.nebius_base_url},
                )
            ]
        reachable = await self._check(reasoner.health(), False)
        if not reachable:
            return [
                Capability(
                    id="reasoning",
                    label="Reasoning (Nemotron 3 Ultra via Nebius)",
                    state="broken",
                    detail=(
                        f"Key is present but {settings.nebius_base_url} did not answer."
                        " The legacy api.studio.nebius.com host returns 404 here."
                    ),
                    fix=(
                        "Check network access and that NEBIUS_BASE_URL is "
                        "https://api.tokenfactory.nebius.com/v1"
                    ),
                    env="NEBIUS_BASE_URL",
                    optional=False,
                    meta={"model": settings.nemotron_model},
                )
            ]
        return [
            Capability(
                id="reasoning",
                label="Reasoning (Nemotron 3 Ultra via Nebius)",
                state="live",
                detail=f"{settings.nemotron_model} reachable.",
                optional=False,
                meta={"model": settings.nemotron_model, "base_url": settings.nebius_base_url},
            )
        ]

    async def _research(self) -> list[Capability]:
        available = bool(getattr(self.s.tavily, "available", False))
        return [
            Capability(
                id="research",
                label="Web research (Tavily)",
                state="live" if available else "not_configured",
                detail=(
                    "Search and fetch are available to the research agent."
                    if available
                    else "No key: research requests fall back to whatever the model already knows."
                ),
                fix="" if available else "Set TAVILY_API_KEY in .env.",
                env="TAVILY_API_KEY",
            )
        ]

    async def _embeddings(self) -> list[Capability]:
        embedder = getattr(self.s.memory, "embedder", None)
        name = getattr(embedder, "name", "unknown")
        dim = getattr(embedder, "dim", None)
        remote = name.startswith("nebius")
        return [
            Capability(
                id="embeddings",
                label="Embeddings",
                state="live",
                detail=f"{name}{f' (dim {dim})' if dim else ''}.",
                fix="" if remote else "Set NEBIUS_API_KEY to use hosted embeddings instead of the local hash embedder.",
                env="" if remote else "NEBIUS_API_KEY",
                meta={"embedder": name, "dim": dim},
            )
        ]

    async def _record_store(self) -> list[Capability]:
        name = str(getattr(self.s.store, "name", "unknown"))
        degraded = bool(getattr(self.s.store, "degraded", False))
        if name.startswith("mongodb"):
            state = "stale" if degraded else "live"
            detail = (
                "MongoDB request failed at runtime; writes are going to the local file store."
                if degraded
                else "MongoDB is serving all reads and writes."
            )
            return [
                Capability(
                    id="record_store",
                    label="Record store (MongoDB)",
                    state=state,
                    detail=detail,
                    fix="Check MONGODB_URI and that the server is running." if degraded else "",
                    env="MONGODB_URI",
                    meta={"store": name},
                )
            ]
        return [
            Capability(
                id="record_store",
                label="Record store",
                state="live",
                detail=f"{name} (local JSON, no server needed).",
                fix="Set MONGODB_URI to switch to MongoDB.",
                env="MONGODB_URI",
                optional=False,
                meta={"store": name},
            )
        ]

    async def _vector_index(self) -> list[Capability]:
        name = str(getattr(self.s.memory.vectors, "name", "unknown"))
        hosted = name.startswith("zilliz") or name.startswith("milvus")
        return [
            Capability(
                id="vector_index",
                label="Vector index",
                state="live",
                detail=f"{name}.",
                fix="" if hosted else "Set ZILLIZ_URI and ZILLIZ_TOKEN for a hosted index.",
                env="" if hosted else "ZILLIZ_URI",
                meta={"vectors": name},
            )
        ]

    async def _agent_runtime(self) -> list[Capability]:
        info = await self._check(self.s.runtime.info(), None)
        mode = getattr(info, "mode", self.settings.agent_runtime)
        name = getattr(info, "name", "local")
        available = bool(getattr(info, "available", False))
        detail = str(getattr(info, "detail", "") or "")
        if available:
            return [Capability(id="agent_runtime", label="Desktop control runtime", state="live", detail=detail or name)]
        return [
            Capability(
                id="agent_runtime",
                label="Desktop control runtime (Hermes CLI)",
                state="not_configured",
                detail=(
                    detail
                    or "Not available: computer_* tools refuse rather than simulate, so nothing is faked."
                ),
                fix=(
                    "Install the Hermes CLI, or set AGENT_RUNTIME=remote with HERMES_ENDPOINT." 
                ),
                env="HERMES_COMMAND",
                meta={"mode": mode, "runtime": name},
            )
        ]

    async def _sandbox(self) -> list[Capability]:
        status = await self._check(self.s.sandbox.status(), None)
        provider = getattr(status, "provider", self.settings.sandbox_provider)
        isolation = getattr(status, "isolation", "none")
        degraded = bool(getattr(status, "degraded", False))
        details = getattr(status, "details", "") or ""
        if isolation and isolation != "none":
            return [
                Capability(
                    id="sandbox",
                    label="Execution isolation",
                    state="stale" if degraded else "live",
                    detail=f"{provider} providing {isolation} isolation.",
                    meta={"provider": provider, "isolation": isolation},
                )
            ]
        return [
            Capability(
                id="sandbox",
                label="Execution isolation",
                state="not_configured",
                detail=(
                    f"{provider}: commands run directly on this machine with in-process policy checks"
                    " only. Nothing is contained at the OS level."
                    + (f" {details}" if details else "")
                ),
                fix=(
                    "Set SANDBOX_PROVIDER=nemoclaw with the NemoClaw gateway reachable"
                    " (OPENSHIELD_GATEWAY_URL) if you want kernel-level containment."
                ),
                env="SANDBOX_PROVIDER",
                optional=False,
                meta={"provider": provider, "isolation": isolation},
            )
        ]

    async def _screen(self) -> list[Capability]:
        mode = self.settings.screen_capture
        return [
            Capability(
                id="screen_capture",
                label="Screen capture",
                state="live" if mode != "off" else "declined",
                detail=f"mode: {mode}.",
                fix="" if mode != "off" else "Set SCREEN_CAPTURE=on_demand to re-enable.",
                env="SCREEN_CAPTURE",
            )
        ]

    async def _ocr(self) -> list[Capability]:
        if not self.settings.ocr_enabled:
            return [
                Capability(
                    id="ocr",
                    label="Local OCR",
                    state="declined",
                    detail="Disabled by configuration.",
                    fix="Set OCR_ENABLED=true.",
                    env="OCR_ENABLED",
                )
            ]
        has_tesseract = _has_binary("tesseract") or _has_module("pytesseract")
        if has_tesseract:
            return [Capability(id="ocr", label="Local OCR", state="live", detail="tesseract available for offline text extraction.")]
        return [
            Capability(
                id="ocr",
                label="Local OCR",
                state="not_configured",
                detail="No tesseract found, so screen text falls back to the vision model when one is set.",
                fix="Install the Tesseract binary, or `uv sync --extra ocr` and install the engine.",
                env="OCR_LANGUAGE",
            )
        ]

    async def _voice(self) -> list[Capability]:
        from app.voice import probe_voice

        info = probe_voice(self.settings)
        if not self.settings.voice_enabled:
            return [
                Capability(
                    id="voice",
                    label="Spoken answers",
                    state="declined",
                    detail=f"Off by default. Engine available: {'yes' if info.get('engine') else 'no'}.",
                    fix="Set VOICE_ENABLED=true, then POST /voice/speak to test it.",
                    env="VOICE_ENABLED",
                    meta=info,
                )
            ]
        if info.get("engine"):
            return [
                Capability(id="voice", label="Spoken answers", state="live", detail=str(info.get("detail", "")), meta=info)
            ]
        return [
            Capability(
                id="voice",
                label="Spoken answers",
                state="broken",
                detail=f"Voice is enabled but no working speech engine was found on {platform.system()}.",
                fix=str(info.get("fix", "Install a system speech engine.")),
                env="VOICE_ENABLED",
                meta=info,
            )
        ]

    async def _identity(self) -> list[Capability]:
        identity = getattr(self.s, "identity", None)
        if identity is None:
            return [
                Capability(
                    id="identity",
                    label="Identity documents",
                    state="broken",
                    detail="The identity layer is not wired into services.",
                    fix="Restart the backend; this indicates a partial upgrade.",
                    optional=False,
                )
            ]
        summary = await self._check(identity.summarise(), {})
        docs = summary.get("documents", {}) if isinstance(summary, dict) else {}
        axioms = list(summary.get("axioms", [])) if isinstance(summary, dict) else []
        missing = [name for name, info in docs.items() if not info.get("chars")]
        if missing:
            return [
                Capability(
                    id="identity",
                    label="Identity documents",
                    state="not_configured",
                    detail=f"Not yet seeded: {', '.join(missing)}.",
                    fix="Restart the backend, or open the Identity page and save a template.",
                    meta={"documents": docs},
                )
            ]
        return [
            Capability(
                id="identity",
                label="Identity documents",
                state="live",
                detail=f"GENOME.md, TELOS.md and MEMORY.md present; {len(axioms)} axiom(s) loaded.",
                meta={"documents": docs, "axioms": len(axioms)},
            )
        ]

    async def _axiom_reconciliation(self) -> list[Capability]:
        """The check that matters most: an axiom written but not enforced looks identical to a working one."""
        identity = getattr(self.s, "identity", None)
        interceptors = getattr(self.s, "interceptors", None)
        if identity is None or interceptors is None:
            return []
        summary = await self._check(identity.summarise(), {})
        documents = summary.get("documents", {}) if isinstance(summary, dict) else {}
        genome_chars = int((documents.get("GENOME.md") or {}).get("chars", 0) or 0)
        rules = [rule for rule in interceptors.rules if rule.source == "genome"]
        if not genome_chars:
            return []
        if rules:
            detail = f"{len(rules)} enforceable rule(s) compiled from GENOME.md."
            if interceptors.errors:
                detail += f" {len(interceptors.errors)} rule problem(s)."
            return [
                Capability(
                    id="axiom_enforcement",
                    label="Axiom enforcement",
                    state="stale" if interceptors.errors else "live",
                    detail=detail,
                    fix="Fix the JSON in the dobot-rules block." if interceptors.errors else "",
                    meta={"rules": [rule.id for rule in rules], "errors": interceptors.errors},
                )
            ]
        return [
            Capability(
                id="axiom_enforcement",
                label="Axiom enforcement",
                state="not_configured",
                detail=(
                    "GENOME.md has prose axioms but no ```dobot-rules block, so your rules are advisory:"
                    " followed by the model when it chooses to."
                ),
                fix="Add a ```dobot-rules JSON block to GENOME.md so the axioms become code paths.",
                meta={"genome_chars": genome_chars},
            )
        ]

    async def _skill_reconciliation(self) -> list[Capability]:
        """Compare skills on disk against skills actually loaded, because a silently skipped skill is a lie."""
        skills = getattr(self.s, "skills", None)
        if skills is None:
            return []
        loaded = skills.records()
        loaded_names = {record.name for record in loaded}
        directory = self.settings.skills_dir
        on_disk: set[str] = set()
        try:
            if directory.exists():
                for child in sorted(directory.iterdir()):
                    if child.is_dir() and (child / "SKILL.md").exists():
                        on_disk.add(child.name)
        except OSError:
            on_disk = set()
        loaded_lower = {name.lower() for name in loaded_names}
        missing = sorted(name for name in on_disk if name.lower() not in loaded_lower and name not in loaded_names)
        if missing:
            return [
                Capability(
                    id="skill_inventory",
                    label="Skill inventory",
                    state="stale",
                    detail=f"On disk but not loaded: {', '.join(missing)}.",
                    fix="Check each skill's SKILL.md front-matter (name and description are required).",
                    meta={"on_disk": sorted(on_disk), "loaded": sorted(loaded_names)},
                )
            ]
        return [
            Capability(
                id="skill_inventory",
                label="Skill inventory",
                state="live",
                detail=f"{len(loaded_names)} skill(s) loaded and scanned.",
                meta={"loaded": sorted(loaded_names)},
            )
        ]

    async def _interceptors(self) -> list[Capability]:
        interceptors = getattr(self.s, "interceptors", None)
        if interceptors is None:
            return []
        summary = interceptors.summarise()
        return [
            Capability(
                id="interceptors",
                label="Pre-action interceptors",
                state=("live" if summary["count"] else "not_configured"),
                detail=(
                    f"{summary['count']} rule(s) across {len(summary['by_source'])} source(s)."
                    if summary["count"]
                    else "No rules defined. Skills and GENOME.md can both contribute rules."
                ),
                fix="" if summary["count"] else "Add a dobot-rules block to GENOME.md.",
                meta={"count": summary["count"], "by_source": summary["by_source"]},
            )
        ]

    async def _skill_scanner(self) -> list[Capability]:
        skills = getattr(self.s, "skills", None)
        if skills is None:
            return []
        mode = self.settings.skill_scan_mode
        blocked = skills.blocked_names()
        flagged = skills.flagged_names()
        if mode == "off":
            return [
                Capability(
                    id="skill_scanner",
                    label="Skill scanner",
                    state="declined",
                    detail="Scanning is off, so skill content is loaded without inspection.",
                    fix="Set SKILL_SCAN_MODE=block.",
                    env="SKILL_SCAN_MODE",
                )
            ]
        return [
            Capability(
                id="skill_scanner",
                label="Skill scanner",
                state="live",
                detail=(
                    f"mode={mode}; {len(flagged)} flagged, {len(blocked)} blocked."
                    if flagged
                    else f"mode={mode}; every skill clean."
                ),
                meta={"flagged": flagged, "blocked": blocked},
            )
        ]

    async def _consolidation(self) -> list[Capability]:
        consolidator = getattr(self.s, "consolidator", None)
        if consolidator is None:
            return []
        if not self.settings.consolidation_enabled:
            return [
                Capability(
                    id="consolidation",
                    label="Memory consolidation",
                    state="declined",
                    detail="Disabled, so memories never decay, merge, or get promoted.",
                    fix="Set CONSOLIDATION_ENABLED=true.",
                    env="CONSOLIDATION_ENABLED",
                )
            ]
        last = consolidator.last_report
        return [
            Capability(
                id="consolidation",
                label="Memory consolidation",
                state="live" if last else "stale",
                detail=(
                    f"Last pass: {last.summary_line()}."
                    if last
                    else f"Scheduled every {consolidator.interval_seconds}s; no pass has run yet."
                ),
                fix="" if last else "POST /system/consolidate to run one now.",
                meta={"last": last.as_dict() if last else None, "interval_seconds": consolidator.interval_seconds},
            )
        ]

    async def _ledger(self) -> list[Capability]:
        canonical = getattr(self.s, "canonical", None)
        if canonical is None:
            return []
        stats = await self._check(canonical.stats(), {})
        active = int(stats.get("active", 0)) if isinstance(stats, dict) else 0
        return [
            Capability(
                id="canonical_ledger",
                label="Canonical facts ledger",
                state="live" if active else "not_configured",
                detail=(
                    f"{active} resident fact(s), average confidence {stats.get('avg_confidence', 0)}."
                    if active
                    else "Empty. Facts are promoted here once they have been stated repeatedly."
                ),
                fix="" if active else "State a durable fact twice, or POST /system/consolidate.",
                meta=dict(stats) if isinstance(stats, dict) else {},
            )
        ]

    async def _budgets(self) -> list[Capability]:
        juice = getattr(self.s, "tokenjuice", None)
        summary = juice.summary() if juice is not None else {}
        priced = self.settings.pricing_configured
        return [
            Capability(
                id="tokenjuice",
                label="Prompt compression (TokenJuice)",
                state="live" if summary.get("enabled") else "declined",
                detail=(
                    f"{summary.get('calls', 0)} blob(s) processed, {summary.get('saved_chars', 0)} chars saved "
                    f"({summary.get('saved_pct', 0)}%)."
                    if summary.get("enabled")
                    else "Disabled, so full tool output is sent to the model."
                ),
                meta=summary,
            ),
            Capability(
                id="cost_pricing",
                label="Cost estimation",
                state="live" if priced else "not_configured",
                detail=(
                    "Prices configured; spend is estimated from token counts."
                    if priced
                    else "Tokens are counted but no price is set, so cost is reported as unknown rather than guessed."
                ),
                fix="Set PRICE_PER_MTOK_INPUT and PRICE_PER_MTOK_OUTPUT to see estimated spend.",
                env="PRICE_PER_MTOK_INPUT",
            ),
        ]

    async def _api_auth(self) -> list[Capability]:
        required = self.settings.api_auth_required
        return [
            Capability(
                id="api_auth",
                label="Local API authentication",
                state="live" if required else "not_configured",
                detail=(
                    "A bearer token is required on every request except /health."
                    if required
                    else "The loopback API is open. That is fine for a single-user laptop and unsafe on shared Wi-Fi."
                ),
                fix="" if required else "Set DOBOT_API_TOKEN to require a bearer token.",
                env="DOBOT_API_TOKEN",
                optional=False,
            )
        ]

    async def _kill_switch(self) -> list[Capability]:
        active = self.s.kill_switch.active_ids
        return [
            Capability(
                id="kill_switch",
                label="Kill switch",
                state="live",
                detail=f"{self.settings.kill_switch_hotkey}; {len(active)} task(s) currently cancellable.",
                optional=False,
                meta={"active_tasks": active},
            )
        ]

    async def _laya(self) -> list[Capability]:
        """The judgement model behind JEV. Heuristic-only is honest but worth knowing about."""
        laya = getattr(self.s.decision.jev, "laya", None)
        if laya is None:
            return []
        health = await self._check(laya.health(), {})
        mode = str(health.get("mode", "heuristic"))
        if mode == "server":
            reachable = bool(health.get("reachable"))
            return [
                Capability(
                    id="laya",
                    label="Judgement model (Laya server)",
                    state="live" if reachable else "broken",
                    detail=(
                        f"laya-serve reachable at {health.get('server_url')} — calibrated, model-backed JEV."
                        if reachable
                        else f"{health.get('server_url')} did not answer, so JEV falls back to heuristics per action."
                    ),
                    fix="" if reachable else "Start laya-serve, or clear LAYA_SERVER_URL to silence this check.",
                    env="LAYA_SERVER_URL",
                    meta=health,
                )
            ]
        if mode == "inprocess":
            loaded = bool(health.get("reachable"))
            return [
                Capability(
                    id="laya",
                    label="Judgement model (Laya in-process)",
                    state="live" if loaded else "broken",
                    detail=(
                        "The laya package is loaded in this process."
                        if loaded
                        else "LAYA_INPROCESS is set but the laya package is not installed."
                    ),
                    fix="" if loaded else "Run `uv add laya` (or `pip install laya`) and restart.",
                    env="LAYA_INPROCESS",
                    meta=health,
                )
            ]
        return [
            Capability(
                id="laya",
                label="Judgement model (Laya)",
                state="not_configured",
                detail=(
                    "JEV is judging with deterministic heuristics only. The open Laya model adds "
                    "calibrated probabilities for risk, urgency and sensitivity."
                ),
                fix=(
                    'Run `pip install "laya[serve]" && laya-serve`, set LAYA_SERVER_URL=http://127.0.0.1:8000, '
                    "and restart — or set LAYA_INPROCESS=1 with the laya package installed."
                ),
                env="LAYA_SERVER_URL",
                meta=health,
            )
        ]


    async def _harness(self) -> list[Capability]:
        """The structured step-execution loop and the lifecycle graph, in one honest probe."""
        from app.core.harness import harness_engine
        from app.core.task_graph import orchestration_engine

        engine = harness_engine(self.settings)
        lifecycle = orchestration_engine(self.settings)
        if engine == "langgraph" and lifecycle == "langgraph":
            return [
                Capability(
                    id="harness",
                    label="Agent harness (LangGraph)",
                    state="live",
                    detail=(
                        "Steps run through the validate→execute→finalise state graph, and the whole "
                        "task lifecycle (see→understand→decide→act) is a LangGraph state machine."
                    ),
                    meta={"engine": engine, "lifecycle": lifecycle},
                )
            ]
        detail = (
            f"Step harness: {engine}. Lifecycle graph: {lifecycle}. "
            "Outcomes are identical to the graph engines by construction; the graph adds "
            "inspectability and replayability."
        )
        fix = (
            "cd backend && uv pip install langgraph"
            if not (engine == "langgraph" or lifecycle == "langgraph")
            else ""
        )
        return [
            Capability(
                id="harness",
                label="Agent harness",
                state="live" if (engine == "langgraph" or lifecycle == "langgraph") else "not_configured",
                detail=detail,
                fix=fix,
                env="LANGGRAPH_ENABLED",
                meta={"engine": engine, "lifecycle": lifecycle},
            )
        ]

    async def _langsmith(self) -> list[Capability]:
        """Optional hosted tracing. Off is a supported state, not a fault."""
        from app.observability.langsmith import langsmith_available

        if not self.settings.langsmith_api_key.strip():
            return [
                Capability(
                    id="langsmith",
                    label="Usage tracing (LangSmith)",
                    state="declined",
                    detail=(
                        "Off. Model calls and harness stages are still recorded in the local run "
                        "journal and cost ledger."
                    ),
                    fix=(
                        "Set LANGSMITH_API_KEY (smith.langchain.com) to make every call searchable "
                        "with token counts and latency."
                    ),
                    env="LANGSMITH_API_KEY",
                )
            ]
        if not langsmith_available():
            return [
                Capability(
                    id="langsmith",
                    label="Usage tracing (LangSmith)",
                    state="broken",
                    detail="LANGSMITH_API_KEY is set but the langsmith package is not installed.",
                    fix="cd backend && uv pip install langsmith",
                    env="LANGSMITH_API_KEY",
                )
            ]
        return [
            Capability(
                id="langsmith",
                label="Usage tracing (LangSmith)",
                state="live",
                detail=f"Tracing to project '{self.settings.langsmith_project}'.",
                env="LANGSMITH_API_KEY",
            )
        ]


    async def _deep_research(self) -> list[Capability]:
        """The optional deepagents research subagent."""
        from app.agents.deep_agent import deepagents_ready

        ok, why = deepagents_ready(self.settings)
        if ok:
            return [
                Capability(
                    id="deep_research",
                    label="Deep research (deepagents)",
                    state="live",
                    detail="The planner may run the deepagents research subagent for multi-search, cited reports.",
                    env="DEEPAGENTS_ENABLED",
                )
            ]
        declined = why.startswith("DEEPAGENTS_ENABLED")
        return [
            Capability(
                id="deep_research",
                label="Deep research (deepagents)",
                state="declined" if declined else "not_configured",
                detail=f"Off: {why}.",
                fix=(
                    "Set DEEPAGENTS_ENABLED=1 to let the planner use deep research."
                    if declined
                    else f"{why}. Set DEEPAGENTS_ENABLED=1 once resolved."
                ),
                env="DEEPAGENTS_ENABLED",
            )
        ]


    async def _transcriber(self) -> list[Capability]:
        """Speech-to-text. Optional; each missing piece gets its exact fix."""
        from app.agents.transcriber import get_transcriber

        info = get_transcriber().probe()
        state = str(info.get("state", "declined"))
        if state == "live":
            detail = "Local mic transcription ready"
            if info.get("loaded"):
                detail += f" (model loaded, device={info.get('device', 'auto')})"
            elif not info.get("ffmpeg"):
                detail += " — browser recordings (webm/ogg) need ffmpeg on PATH"
            if info.get("note"):
                detail += f" — {info['note']}"
            return [
                Capability(
                    id="transcriber",
                    label="Speech-to-text (local Whisper)",
                    state="live",
                    detail=detail,
                    env="TRANSCRIBER_ENABLED",
                    meta=info,
                )
            ]
        if state == "broken":
            return [
                Capability(
                    id="transcriber",
                    label="Speech-to-text (local Whisper)",
                    state="broken",
                    detail=str(info.get("detail", "model failed to load")),
                    fix="Check TRANSCRIBER_DEVICE (try TRANSCRIBER_DEVICE=cpu) and free memory, then restart.",
                    env="TRANSCRIBER_ENABLED",
                    meta=info,
                )
            ]
        if state == "not_configured":
            return [
                Capability(
                    id="transcriber",
                    label="Speech-to-text (local Whisper)",
                    state="not_configured",
                    detail="TRANSCRIBER_ENABLED is on, but the whisper package is not installed.",
                    fix="cd backend && uv sync --extra voice (and ffmpeg for webm/ogg).",
                    env="TRANSCRIBER_ENABLED",
                    meta=info,
                )
            ]
        return [
            Capability(
                id="transcriber",
                label="Speech-to-text (local Whisper)",
                state="declined",
                detail="Off. The chat and widget mic buttons say so instead of pretending.",
                fix="Set TRANSCRIBER_ENABLED=1 (and install the whisper package) to enable voice input.",
                env="TRANSCRIBER_ENABLED",
                meta=info,
            )
        ]


def build_doctor(services: Any) -> Doctor:
    return Doctor(services)
