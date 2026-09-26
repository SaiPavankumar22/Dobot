"""Activity log.

Every event on the bus is persisted, so the timeline the user sees is the same record the agent
produced — not a nicer summary of it. This is what makes "never hide what it is doing" verifiable.
"""

from __future__ import annotations

import asyncio

from app.events import DobotEvent, EventBus, get_event_bus
from app.logging_setup import get_logger
from app.memory.store import RecordStore
from app.schemas import ActivityRecord

logger = get_logger(__name__)

#: Events that are noise in a user-facing timeline.
SKIP_TYPES = {"dot_status"}


class ActivityLog:
    def __init__(self, store: RecordStore, bus: EventBus | None = None, *, persist: bool = True) -> None:
        self.store = store
        self.bus = bus or get_event_bus()
        self.persist = persist
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._consume(), name="dobot-activity-log")

    async def _consume(self) -> None:
        async with self.bus.subscription() as queue:
            while True:
                event: DobotEvent = await queue.get()
                if event.type.value in SKIP_TYPES or not self.persist:
                    continue
                record = ActivityRecord(
                    task_id=event.task_id,
                    event_type=event.type.value,
                    message=event.message[:500],
                    metadata={key: value for key, value in event.data.items() if key != "plan"},
                    timestamp=event.timestamp,
                )
                try:
                    await self.store.insert("activity", record.model_dump(mode="json"))
                except Exception as exc:  # noqa: BLE001 - logging must never break the loop
                    logger.warning("could not persist activity (%s)", exc)

    async def for_task(self, task_id: str, limit: int = 300) -> list[ActivityRecord]:
        docs = await self.store.all("activity", limit=2000)
        records: list[ActivityRecord] = []
        for doc in docs:
            if doc.get("task_id") != task_id:
                continue
            try:
                records.append(ActivityRecord.model_validate(doc))
            except Exception:  # noqa: BLE001
                continue
        records.sort(key=lambda record: record.timestamp)
        return records[-limit:]

    async def recent(self, limit: int = 100) -> list[ActivityRecord]:
        docs = await self.store.all("activity", limit=limit)
        records: list[ActivityRecord] = []
        for doc in docs:
            try:
                records.append(ActivityRecord.model_validate(doc))
            except Exception:  # noqa: BLE001
                continue
        records.sort(key=lambda record: record.timestamp, reverse=True)
        return records[:limit]

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
