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
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings, get_settings
from app.core.schedules import (  # noqa: F401 - re-exported: callers import it from here
    catch_up_grace_seconds,
    parse_schedule,
    validate_cron,
)
from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.memory.store import RecordStore
from app.schemas import AutomationKind, AutomationRecord

logger = get_logger(__name__)

Runner = Callable[[str, str], Awaitable[Any]]  # (prompt, automation_id) -> anything


def _current_user() -> str:
    """The requesting user's id (multi-user), else the legacy single-user marker."""
    from app.core.tenancy import current_user_id

    return current_user_id.get() or "local"

_DURATION = re.compile(r"^(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours|d|day|days)$", re.I)
#: One-shot catch-up window while restoring (Hermes clamps its grace to 2 minutes – 2 hours).
_ONESHOT_GRACE_SECONDS = 7200
#: How far a restore walks a trigger looking for runs Dobot slept through. A 5-minute job that was
#: off for a week is 2016 occurrences; past this the walk stops and the run counts as missed.
_CATCH_UP_STEPS = 500


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
                continue
            # A run Dobot slept through, still fresh enough to be worth firing now. Exactly once:
            # accumulated misses are skipped, as in Hermes' catch-up rule.
            missed = self._missed_run(record)
            if missed is not None:
                logger.info(
                    "catching up %s: the run due %s was missed while Dobot was off",
                    record.id,
                    missed.isoformat(),
                )
                asyncio.create_task(self._fire(record.id, record.prompt))
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
        if kind is AutomationKind.REMINDER:
            fire_at = parse_when(schedule)
            record = AutomationRecord(
                name=name.strip()[:120],
                schedule=fire_at.isoformat(),
                prompt=prompt.strip(),
                kind=AutomationKind.REMINDER,
                status="active",
                next_run_at=fire_at,
                user_id=_current_user(),
            )
        else:
            # Accepts everything `parse_schedule` does — cron, 'every monday 9am', 'every 30m',
            # 'in 30m', a timestamp — and raises ValueError with the help text if it cannot read it.
            parsed = parse_schedule(schedule)
            # A one-shot stored as "in 30m" would drift by the downtime on every restart, so it is
            # persisted as the instant it resolved to. Recurring forms keep the user's own words.
            stored = parsed.run_at.isoformat() if parsed.kind == "once" else schedule.strip()
            record = AutomationRecord(
                name=name.strip()[:120],
                schedule=stored,
                prompt=prompt.strip(),
                kind=AutomationKind.CRON,
                status="active",
                user_id=_current_user(),
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
            user_id=_current_user(),
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
            trigger: CronTrigger | DateTrigger | IntervalTrigger = DateTrigger(run_date=fire_at)
        else:
            parsed = parse_schedule(record.schedule)
            if parsed.kind == "cron":
                trigger = CronTrigger.from_crontab(parsed.cron)
            elif parsed.kind == "interval":
                trigger = IntervalTrigger(minutes=parsed.minutes)
            else:
                trigger = DateTrigger(run_date=parsed.run_at)
        self._scheduler.add_job(
            self._fire,
            trigger=trigger,
            args=[record.id, record.prompt],
            id=record.id,
            replace_existing=True,
        )

    def _is_one_shot(self, record: AutomationRecord) -> bool:
        """Reminders and one-off automations: they fire once and must never be re-armed."""
        if record.kind is AutomationKind.REMINDER:
            return True
        try:
            return not parse_schedule(record.schedule).recurring
        except ValueError:
            return False

    def _missed_run(
        self, record: AutomationRecord, *, now: datetime | None = None
    ) -> datetime | None:
        """The newest run Dobot slept through — but only when it is still fresh enough to fire.

        Fresh means inside the catch-up grace window: half a period, clamped to 2 minutes – 2 hours
        (Hermes' rule), with one-shots getting the 2-hour ceiling. Anything older is skipped rather
        than fired late — a three-day-old reminder is worse than no reminder at all. Returns the
        instant to fire at, or ``None`` when there is nothing worth catching up.
        """
        now = now or datetime.now(UTC)

        if self._is_one_shot(record):
            try:
                if record.kind is AutomationKind.REMINDER:
                    target = parse_when(record.schedule)
                else:
                    target = parse_schedule(record.schedule).run_at
            except ValueError:
                return None
            if target is None:
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=now.tzinfo)
            lateness = (now - target).total_seconds()
            return target if 0 < lateness <= _ONESHOT_GRACE_SECONDS else None

        job = self._scheduler.get_job(record.id)
        trigger = job.trigger if job else None
        if trigger is None:
            return None
        anchor = record.last_run_at or record.created_at
        if anchor is None:
            return None
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=now.tzinfo)
        timezone = getattr(trigger, "timezone", None) or now.tzinfo
        cursor = anchor.astimezone(timezone)
        end = now.astimezone(timezone)

        missed: datetime | None = None
        for _ in range(_CATCH_UP_STEPS):
            following = trigger.get_next_fire_time(cursor, cursor + timedelta(microseconds=1))
            if following is None or following > end:
                break
            missed = following
            cursor = following
        if missed is None:
            return None

        next_after = trigger.get_next_fire_time(missed, missed + timedelta(microseconds=1))
        cadence = (next_after - missed).total_seconds() if next_after else None
        lateness = (now - missed.astimezone(UTC)).total_seconds()
        return missed if lateness <= catch_up_grace_seconds(cadence) else None

    # ------------------------------------------------------------------ firing

    async def _fire(self, automation_id: str, prompt: str) -> None:
        if automation_id in self._running:
            logger.info("automation %s is already running; skipping overlap", automation_id)
            return
        self._running.add(automation_id)
        # Multi-user: the job fires outside any request, so re-enter the owner's scope —
        # their memory, approvals and tools, never "nobody's". Single-user: user_id is
        # "local", and as_user("local") still isolates nothing because scoping is keyed
        # on a non-empty id that no request ever sets in single-user mode.
        from app.core.tenancy import as_user

        record = await self.get(automation_id)
        owner = (record.user_id if record else "") or ""
        owner = "" if owner == "local" else owner
        try:
            with as_user(owner):
                # Emitted inside the owner's scope so the event is stamped theirs —
                # subscribers only ever see their own automation runs.
                await self.bus.emit(
                    EventType.AUTOMATION_RUN,
                    message=f"Scheduled task started: {(record.name if record else prompt)[:80]}",
                    automation_id=automation_id,
                    prompt=prompt,
                )
                await self.runner(prompt, automation_id)
        except Exception as exc:  # noqa: BLE001 - a failing automation must not kill the scheduler
            logger.warning("automation %s failed (%s)", automation_id, exc)
            with as_user(owner):
                await self.bus.emit(
                    EventType.FAILED,
                    message=f"Scheduled task failed: {exc}",
                    automation_id=automation_id,
                )
        finally:
            self._running.discard(automation_id)
            with as_user(owner):
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
        # One-shots are done after their single run; recurring jobs keep their active status.
        if self._is_one_shot(record):
            patch["status"] = "completed"
        await self.store.update("automations", automation_id, patch)

    def next_run_at(self, automation_id: str) -> datetime | None:
        job = self._scheduler.get_job(automation_id)
        if job is None or job.next_run_time is None:
            return None
        return job.next_run_time    # ------------------------------------------------------------------ queries
    def _visible(self, record: AutomationRecord) -> bool:
        """Owner filter for API reads.

        Empty context (single-user mode, or background ``_restore``/``_fire``) sees everything.
        An authenticated request sees its own automations plus ``local`` — the legacy records that
        predate accounts, which are shared by the deployment rather than owned by nobody.
        """
        from app.core.tenancy import current_user_id

        uid = current_user_id.get()
        if not uid:
            return True
        return record.user_id == uid or record.user_id == "local"

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
        if not self._visible(record):
            return None
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
            if not self._visible(record):
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
        if await self.get(automation_id) is None:
            return False  # not found, or not this caller's to delete
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
