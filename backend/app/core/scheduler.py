"""Scheduler: reminders and recurring automations.

Scheduled work is ordinary work: it runs through the same context, decision engine, approvals and
verification as a chat request. A scheduled task that needs approval raises one and waits, rather than
quietly bypassing the gate because nobody was watching.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.config import Settings, get_settings
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.memory.store import RecordStore
from app.schemas import AutomationKind, AutomationRecord

logger = get_logger(__name__)

Runner = Callable[[str, str], Awaitable[Any]]  # (prompt, automation_id) -> anything

_DURATION = re.compile(r"^(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours|d|day|days)$", re.I)
_FIELD_COUNT = 5


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """Parse an ISO timestamp, a relative delay ('2h', '30m'), or 'tomorrow at 8pm' style hints."""
    now = now or datetime.now()
    text = value.strip()
    if not text:
        raise ValueError("empty schedule")

    duration = _DURATION.match(text)
    if duration:
        amount = int(duration.group(1))
        unit = duration.group(2).lower()
        if unit.startswith("s"):
            delta = timedelta(seconds=amount)
        elif unit.startswith("m"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("h"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        return now + delta

    lowered = text.lower()
    time_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lowered)
    if any(word in lowered for word in ("tomorrow", "tonight", "at ", "am", "pm", "morning", "evening", "noon")) and time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        meridiem = time_match.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem and "evening" in lowered and hour < 12:
            hour += 12
        elif not meridiem and "morning" in lowered and hour == 12:
            hour = 0
        target = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if "tomorrow" in lowered:
            target += timedelta(days=1)
        elif target <= now:
            target += timedelta(days=1)
        return target

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cannot interpret schedule '{value}'") from exc
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def validate_cron(expression: str) -> str:
    fields = expression.split()
    if len(fields) != _FIELD_COUNT:
        raise ValueError(f"cron expression needs {_FIELD_COUNT} fields, got {len(fields)}")
    CronTrigger.from_crontab(expression)  # raises on invalid syntax
    return expression


class Scheduler:
    def __init__(
        self,
        *,
        store: RecordStore,
        runner: Runner,
        bus: EventBus | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store
        self.runner = runner
        self.bus = bus or get_event_bus()
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._running: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        await self._restore()

    async def _restore(self) -> int:
        count = 0
        for record in await self.list():
            if record.status != "active":
                continue
            try:
                self._schedule_record(record)
                count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not restore automation %s (%s)", record.id, exc)
        if count:
            logger.info("restored %d scheduled job(s)", count)
        return count

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------ create

    async def create_automation(
        self, *, name: str, schedule: str, prompt: str, kind: AutomationKind = AutomationKind.CRON
    ) -> AutomationRecord:
        validate_cron(schedule)
        record = AutomationRecord(
            name=name.strip()[:120],
            schedule=schedule.strip(),
            prompt=prompt.strip(),
            kind=AutomationKind.CRON,
            status="active",
        )
        self._schedule_record(record)
        await self._persist(record)
        return record

    async def create_reminder(self, text: str, when: str) -> AutomationRecord:
        fire_at = parse_when(when)
        record = AutomationRecord(
            name=text.strip()[:120],
            prompt=text.strip(),
            kind=AutomationKind.REMINDER,
            schedule=fire_at.isoformat(),
            status="active",
            next_run_at=fire_at,
        )
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=fire_at),
            args=[record.id, text],
            id=record.id,
            replace_existing=True,
        )
        await self._persist(record)
        return record

    def _schedule_record(self, record: AutomationRecord) -> None:
        if record.kind is AutomationKind.REMINDER:
            try:
                fire_at = parse_when(record.schedule)
            except ValueError:
                return
            self._scheduler.add_job(
                self._fire,
                trigger=DateTrigger(run_date=fire_at),
                args=[record.id, record.prompt],
                id=record.id,
                replace_existing=True,
            )
        else:
            self._scheduler.add_job(
                self._fire,
                trigger=CronTrigger.from_crontab(record.schedule),
                args=[record.id, record.prompt],
                id=record.id,
                replace_existing=True,
            )

    # ------------------------------------------------------------------ firing

    async def _fire(self, automation_id: str, prompt: str) -> None:
        if automation_id in self._running:
            logger.info("automation %s is already running; skipping overlap", automation_id)
            return
        self._running.add(automation_id)
        record = await self.get(automation_id)
        await self.bus.emit(
            EventType.AUTOMATION_RUN,
            message=f"Scheduled task started: {(record.name if record else prompt)[:80]}",
            automation_id=automation_id,
            prompt=prompt,
        )
        try:
            await self.runner(prompt, automation_id)
        except Exception as exc:  # noqa: BLE001 - a failing automation must not kill the scheduler
            logger.warning("automation %s failed (%s)", automation_id, exc)
            await self.bus.emit(
                EventType.FAILED,
                message=f"Scheduled task failed: {exc}",
                automation_id=automation_id,
            )
        finally:
            self._running.discard(automation_id)
            await self._touch(automation_id)

    async def _touch(self, automation_id: str) -> None:
        record = await self.get(automation_id)
        if record is None:
            return
        next_run = self.next_run_at(automation_id)
        patch = {
            "last_run_at": datetime.now(UTC).isoformat(),
            "run_count": record.run_count + 1,
            "next_run_at": next_run.isoformat() if next_run else None,
        }
        if record.kind is AutomationKind.REMINDER:
            patch["status"] = "completed"
        await self.store.update("automations", automation_id, patch)

    def next_run_at(self, automation_id: str) -> datetime | None:
        job = self._scheduler.get_job(automation_id)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time

    # ------------------------------------------------------------------ queries

    async def _persist(self, record: AutomationRecord) -> None:
        next_run = self.next_run_at(record.id)
        if next_run:
            record.next_run_at = next_run
        await self.store.insert("automations", record.model_dump(mode="json"))

    async def get(self, automation_id: str) -> AutomationRecord | None:
        doc = await self.store.get("automations", automation_id)
        if not doc:
            return None
        record = AutomationRecord.model_validate(doc)
        next_run = self.next_run_at(automation_id)
        if next_run:
            record.next_run_at = next_run
        return record

    async def list(self) -> list[AutomationRecord]:
        docs = await self.store.all("automations", limit=500)
        records: list[AutomationRecord] = []
        for doc in docs:
            try:
                record = AutomationRecord.model_validate(doc)
            except Exception:  # noqa: BLE001
                continue
            next_run = self.next_run_at(record.id)
            if next_run:
                record.next_run_at = next_run
            records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    async def set_status(self, automation_id: str, status: str) -> AutomationRecord | None:
        record = await self.get(automation_id)
        if record is None:
            return None
        if status != "active":
            job = self._scheduler.get_job(automation_id)
            if job is not None:
                self._scheduler.remove_job(automation_id)
        else:
            self._schedule_record(record)
        await self.store.update("automations", automation_id, {"status": status})
        return await self.get(automation_id)

    async def delete(self, automation_id: str) -> bool:
        job = self._scheduler.get_job(automation_id)
        if job is not None:
            self._scheduler.remove_job(automation_id)
        return await self.store.delete("automations", automation_id)

    async def run_now(self, automation_id: str) -> None:
        record = await self.get(automation_id)
        if record is None:
            raise KeyError(automation_id)
        asyncio.create_task(self._fire(automation_id, record.prompt))

    def jobs_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self._scheduler.get_jobs()
        ]
