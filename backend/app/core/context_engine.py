"""Context engine.

Assembles everything the reasoning model should know before it plans: the selected screen region, the
most relevant memories, open tasks, available skills, and the environment the agent is running in.
Screen pixels are processed here and never persisted beyond the current task's scratch directory.
"""

from __future__ import annotations

import base64
import platform
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.agents.sandbox import FallbackSandbox
from app.config import Settings, get_settings
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.memory.manager import MemoryManager
from app.memory.store import RecordStore
from app.schemas import (
    ContextBundle,
    EnvironmentSnapshot,
    MemoryHit,
    Region,
    ScreenContext,
    TaskStatus,
)
from app.tools.screen import extract_screen_text, save_ephemeral

logger = get_logger(__name__)

OPEN_STATUSES = {TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_USER}


@dataclass
class ContextRequestData:
    message: str
    region: Region | None = None
    image_base64: str | None = None
    application: str = ""
    window_title: str = ""
    question: str = ""


class ContextEngine:
    def __init__(
        self,
        *,
        memory: MemoryManager,
        store: RecordStore,
        skills: Any = None,
        sandbox: FallbackSandbox | None = None,
        bus: EventBus | None = None,
        settings: Settings | None = None,
        runtime: Any = None,
        identity: Any = None,
        canonical: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.memory = memory
        self.store = store
        self.skills = skills
        self.sandbox = sandbox
        self.runtime = runtime
        # The identity layer and the canonical ledger are injected on every turn with zero model
        # calls, which is the whole point: the things that must never be forgotten should not have to
        # be *found* first.
        self.identity = identity
        self.canonical = canonical
        self.bus = bus or get_event_bus()

    # ------------------------------------------------------------------ screen

    async def build_screen_context(self, request: ContextRequestData) -> ScreenContext | None:
        if not request.image_base64 and request.region is None:
            return None
        png: bytes | None = None
        if request.image_base64:
            try:
                png = base64.b64decode(request.image_base64)
            except Exception as exc:  # noqa: BLE001
                logger.info("could not decode supplied screen image (%s)", exc)
                png = None
        if png is None and request.region is not None:
            try:
                from app.tools.screen import capture_png

                png = capture_png(request.region)
            except Exception as exc:  # noqa: BLE001 - no display, missing deps
                logger.info("server-side capture unavailable (%s)", exc)
                return ScreenContext(
                    application=request.application,
                    window_title=request.window_title,
                    selected_region=request.region,
                    ocr_text="",
                    image_available=False,
                    ocr_engine="unavailable",
                )
        if png is None:
            return None

        text, engine = await extract_screen_text(png, self.settings, request.question)
        ref = save_ephemeral(png, self.settings)
        return ScreenContext(
            application=request.application,
            window_title=request.window_title,
            selected_region=request.region,
            ocr_text=text,
            image_available=True,
            image_ref=str(ref),
            ocr_engine=engine,
            captured_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------ environment

    async def _environment(self) -> EnvironmentSnapshot:
        sandbox_status = None
        if self.sandbox is not None:
            try:
                status = await self.sandbox.status()
                sandbox_status = status
            except Exception:  # noqa: BLE001
                sandbox_status = None
        runtime_info = None
        if self.runtime is not None:
            try:
                runtime_info = await self.runtime.info()
            except Exception:  # noqa: BLE001
                runtime_info = None
        import os

        return EnvironmentSnapshot(
            platform=f"{platform.system()} {platform.release()}",
            cwd=os.getcwd(),
            foreground_app="",
            running_apps=[],
            sandbox_provider=(
                f"{sandbox_status.provider}({sandbox_status.isolation})"
                if sandbox_status
                else self.settings.sandbox_provider
            ),
            agent_runtime=(runtime_info.name if runtime_info else self.settings.agent_runtime),
            screen_capture=self.settings.screen_capture,
            shadow_mode=self.settings.shadow_mode,
        )

    # ------------------------------------------------------------------ bundle

    @staticmethod
    def _tags_for(message: str, screen: ScreenContext | None) -> list[str]:
        tags: list[str] = []
        for token in ("dobot", "project", "work", "report", "research", "downloads"):
            if token in message.lower() or (screen and token in screen.ocr_text.lower()):
                tags.append(token)
        return tags

    async def build(self, request: ContextRequestData, *, task_id: str = "") -> ContextBundle:
        started = time.perf_counter()
        screen = await self.build_screen_context(request)
        tags = self._tags_for(request.message, screen)

        memories: list[MemoryHit] = []
        try:
            memories = await self.memory.recall(request.message, limit=6, task_tags=tags)
        except Exception as exc:  # noqa: BLE001 - retrieval failure must not block a request
            logger.warning("memory retrieval failed (%s)", exc)

        open_tasks: list[dict[str, Any]] = []
        for doc in await self.store.all("tasks", limit=200):
            status = str(doc.get("status", ""))
            if status in {member.value for member in OPEN_STATUSES}:
                open_tasks.append(
                    {
                        "id": doc.get("id"),
                        "title": doc.get("title"),
                        "status": status,
                        "priority": doc.get("priority"),
                    }
                )
        open_tasks = open_tasks[:8]

        standing_rules: list[str] = []
        if self.identity is not None:
            try:
                standing_rules = await self.identity.axioms()
            except Exception as exc:  # noqa: BLE001 - identity is context, never a dependency
                logger.info("identity axioms unavailable (%s)", exc)

        canonical_facts: list[str] = []
        if self.canonical is not None:
            try:
                facts = await self.canonical.active(limit=20)
                canonical_facts = [fact.line(show_confidence=False).removeprefix("- ") for fact in facts]
            except Exception as exc:  # noqa: BLE001
                logger.info("canonical ledger unavailable (%s)", exc)

        skills = self.skills.names() if self.skills is not None else []
        recent: list[str] = []
        try:
            events = self.bus.history(limit=12, task_id=None)
            recent = [f"{event.type.value}: {event.message[:80]}" for event in events if event.message]
        except Exception:  # noqa: BLE001
            recent = []

        bundle = ContextBundle(
            task_id=task_id,
            user_message=request.message,
            screen=screen,
            memories=memories,
            open_tasks=open_tasks,
            skills=skills,
            environment=await self._environment(),
            recent_activity=recent,
            standing_rules=standing_rules,
            canonical_facts=canonical_facts,
        )
        await self.bus.emit(
            EventType.CONTEXT_BUILT,
            message=(
                f"context: {len(memories)} memories"
                + (", screen text" if screen and screen.ocr_text else "")
                + (f", {len(open_tasks)} open tasks" if open_tasks else "")
                + (f", {len(standing_rules)} standing rules" if standing_rules else "")
                + (f", {len(canonical_facts)} canonical facts" if canonical_facts else "")
            ),
            task_id=task_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            screens=[screen.model_dump(mode="json")] if screen else [],
        )
        return bundle
