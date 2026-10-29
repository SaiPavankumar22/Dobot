"""Schedule parsing and catch-up: the natural-language and interval forms Hermes taught us."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from app.core.scheduler import validate_cron
from app.core.schedules import catch_up_grace_seconds, parse_schedule
from app.schemas import AutomationKind


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """An aware wall-clock instant in this machine's zone.

    Cron schedules are interpreted as local wall time (that is what `0 9 * * *` means to a user),
    so catch-up assertions have to be written in the same zone or they will silently test the
    machine's offset instead of the behaviour.
    """
    return datetime(year, month, day, hour, minute).astimezone()


#: Tuesday, one hour after the 09:00 run the catch-up tests talk about.
NOW = local(2026, 11, 3, 10, 0)


def test_natural_phrases_become_cron() -> None:
    assert parse_schedule("every monday 9am", now=NOW).cron == "0 9 * * 1"
    assert parse_schedule("weekdays at 9am", now=NOW).cron == "0 9 * * 1-5"
    assert parse_schedule("every day at 8pm", now=NOW).cron == "0 20 * * *"
    assert parse_schedule("monday, wednesday at 9am", now=NOW).cron == "0 9 * * 1,3"
    assert parse_schedule("every weekend at 10am", now=NOW).cron == "0 10 * * 0,6"


def test_intervals_and_plain_cron_still_work() -> None:
    assert parse_schedule("every 30m", now=NOW).minutes == 30
    assert parse_schedule("2h", now=NOW).minutes == 120
    assert parse_schedule("0 9 * * *", now=NOW).cron == "0 9 * * *"
    assert parse_schedule("*/15 * * * *", now=NOW).cron == "*/15 * * * *"


def test_one_shots_resolve_to_an_instant() -> None:
    assert parse_schedule("in 30m", now=NOW).run_at == NOW + timedelta(minutes=30)
    assert parse_schedule("2026-11-05T09:00:00", now=NOW).run_at == datetime(2026, 11, 5, 9, 0)
    assert parse_schedule("tomorrow at 8pm", now=NOW).run_at == local(2026, 11, 4, 20, 0)


def test_unreadable_schedules_are_rejected_with_help() -> None:
    for value in ("", "every friday", "whenever you feel like it", "0 18 * *"):
        with pytest.raises(ValueError):
            parse_schedule(value, now=NOW)


def test_validate_cron_still_only_accepts_five_fields() -> None:
    assert validate_cron("0 18 * * 5") == "0 18 * * 5"
    with pytest.raises(ValueError):
        validate_cron("every friday")


def test_grace_is_half_a_period_clamped_to_two_minutes_and_two_hours() -> None:
    assert catch_up_grace_seconds(60 * 60 * 24) == 7200  # daily -> 2h ceiling
    assert catch_up_grace_seconds(30 * 60) == 900        # half an hour -> 15 min
    assert catch_up_grace_seconds(60) == 120             # minute -> 2 min floor
    assert catch_up_grace_seconds(None) == 7200


@pytest.mark.asyncio
async def test_natural_schedule_creates_a_live_job(services) -> None:
    record = await services.scheduler.create_automation(
        name="Morning brief", schedule="every day at 9am", prompt="summarise my day"
    )
    assert record.status == "active"
    assert record.next_run_at is not None
    job = services.scheduler._scheduler.get_job(record.id)
    assert job is not None


@pytest.mark.asyncio
async def test_interval_automation_arms_an_interval_trigger(services) -> None:
    record = await services.scheduler.create_automation(
        name="Backups", schedule="every 30m", prompt="back up the workspace"
    )
    job = services.scheduler._scheduler.get_job(record.id)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    assert record.next_run_at is not None


@pytest.mark.asyncio
async def test_api_can_now_create_a_reminder(services) -> None:
    """`kind` used to be accepted and ignored, so a reminder was validated as cron and rejected."""
    record = await services.scheduler.create_automation(
        name="Stand up", schedule="2h", prompt="stretch", kind=AutomationKind.REMINDER
    )
    assert record.kind is AutomationKind.REMINDER
    # Stored as an instant, so re-arming it on restart cannot drift.
    assert datetime.fromisoformat(record.schedule).tzinfo is None
    assert record.next_run_at is not None


@pytest.mark.asyncio
async def test_one_shot_automation_persists_the_instant_it_resolved_to(services) -> None:
    record = await services.scheduler.create_automation(
        name="Once", schedule="in 45m", prompt="do the thing"
    )
    first = parse_schedule(record.schedule, now=NOW).run_at
    second = parse_schedule(record.schedule, now=NOW + timedelta(days=3)).run_at
    assert first is not None and first == second  # idempotent across restarts


@pytest.mark.asyncio
async def test_missed_daily_run_inside_the_grace_window_is_caught_up(services) -> None:
    record = await services.scheduler.create_automation(
        name="Daily", schedule="0 9 * * *", prompt="daily"
    )
    await services.store.update(
        "automations",
        record.id,
        {"last_run_at": local(2026, 11, 2, 9, 0).isoformat()},
    )
    stored = await services.scheduler.get(record.id)
    assert stored is not None

    # 10:00 — today's 09:00 run was missed by an hour, well inside the 2h window.
    caught = services.scheduler._missed_run(stored, now=NOW)
    assert caught is not None
    assert caught.astimezone() == local(2026, 11, 3, 9, 0)

    # 15:00 — the same run is now six hours stale: skip it rather than fire something old.
    too_late = services.scheduler._missed_run(stored, now=local(2026, 11, 3, 15, 0))
    assert too_late is None


@pytest.mark.asyncio
async def test_a_fresh_automation_has_nothing_to_catch_up(services) -> None:
    record = await services.scheduler.create_automation(
        name="Daily", schedule="0 9 * * *", prompt="daily"
    )
    stored = await services.scheduler.get(record.id)
    assert stored is not None
    # Before today's 09:00: the record was just created and the next run is still ahead.
    assert services.scheduler._missed_run(stored, now=local(2026, 10, 29, 8, 0)) is None


@pytest.mark.asyncio
async def test_missed_reminder_is_fresh_within_two_hours_and_stale_after(services) -> None:
    record = await services.scheduler.create_reminder("Drink water", "2026-11-03T09:30:00")
    stored = await services.scheduler.get(record.id)
    assert stored is not None

    assert services.scheduler._missed_run(stored, now=NOW) is not None
    assert services.scheduler._missed_run(stored, now=local(2026, 11, 3, 15, 0)) is None
