"""Event bus: one place where every stage of the loop reports what it is doing.

The desktop client subscribes over WebSocket. Events drive the dot status, the activity timeline,
task progress, approval prompts and research progress, so they are the backbone of observability.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.schemas import DotStatus

logger = get_logger(__name__)


class EventType(str, Enum):
    TASK_RECEIVED = "task_received"
    CONTEXT_BUILT = "context_built"
    PLANNING = "planning"
    PLAN_CREATED = "plan_created"
    DECISION = "decision"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    EXECUTION_STARTED = "execution_started"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    VERIFICATION = "verification"
    MEMORY_WRITTEN = "memory_written"
    RESEARCH_PROGRESS = "research_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    SHADOW_PLAN = "shadow_plan"
    DOT_STATUS = "dot_status"
    NOTIFICATION = "notification"
    TASK_UPDATED = "task_updated"
    AUTOMATION_RUN = "automation_run"


class DobotEvent(BaseModel):
    type: EventType
    task_id: str = ""
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    sequence: int = 0


class EventBus:
    """Fan-out pub/sub with a bounded replay buffer for late subscribers."""

    def __init__(self, replay_size: int = 400) -> None:
        self._subscribers: set[asyncio.Queue[DobotEvent]] = set()
        self._history: deque[DobotEvent] = deque(maxlen=replay_size)
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._dot_status: DotStatus = DotStatus.IDLE

    async def publish(self, event: DobotEvent) -> DobotEvent:
        async with self._lock:
            self._sequence += 1
            event.sequence = self._sequence
        self._history.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow consumer must never stall the agent loop: drop its oldest event.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass
        return event

    async def emit(
        self,
        event_type: EventType,
        message: str = "",
        task_id: str = "",
        **data: Any,
    ) -> DobotEvent:
        return await self.publish(
            DobotEvent(type=event_type, message=message, task_id=task_id, data=data)
        )

    async def set_dot_status(self, status: DotStatus, detail: str = "") -> None:
        self._dot_status = status
        await self.emit(EventType.DOT_STATUS, message=detail, status=status.value)

    @property
    def dot_status(self) -> str:
        """Current status, so a reconnecting client does not render a stale replayed one."""
        return self._dot_status.value

    def subscribe(self) -> asyncio.Queue[DobotEvent]:
        queue: asyncio.Queue[DobotEvent] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[DobotEvent]) -> None:
        self._subscribers.discard(queue)

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[asyncio.Queue[DobotEvent]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    def history(self, limit: int = 100, task_id: str | None = None) -> list[DobotEvent]:
        events = [event for event in self._history if task_id is None or event.task_id == task_id]
        return events[-limit:]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Process-wide bus. Created lazily so tests can build an isolated one."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def set_event_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus
