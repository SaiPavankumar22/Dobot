"""Scheduling: cron validation, time parsing, automation and reminder lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.scheduler import parse_when, validate_cron


def test_validate_cron_accepts_five_fields() -> None:
    assert validate_cron("0 18 * * 5") == "0 18 * * 5"
    assert validate_cron("*/15 * * * *")


def test_validate_cron_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        validate_cron("every friday")
    with pytest.raises(ValueError):
        validate_cron("0 18 * *")


def test_parse_relative_delays() -> None:
    now = datetime(2026, 9, 25, 12, 0, 0)
    assert parse_when("2h", now=now) == now + timedelta(hours=2)
    assert parse_when("30m", now=now) == now + timedelta(minutes=30)
    assert parse_when("45s", now=now) == now + timedelta(seconds=45)


def test_parse_iso_timestamp() -> None:
    parsed = parse_when("2026-10-01T20:00:00")
    assert parsed.year == 2026 and parsed.hour == 20


def test_parse_evening_time_rolls_forward() -> None:
    now = datetime(2026, 9, 25, 21, 0, 0)
    parsed = parse_when("tomorrow at 8pm", now=now)
    assert parsed.day == 26
    assert parsed.hour == 20


def test_parse_rejects_gibberish() -> None:
    with pytest.raises(ValueError):
        parse_when("whenever you feel like it")


@pytest.mark.asyncio
async def test_automation_lifecycle(services) -> None:
    record = await services.scheduler.create_automation(
        name="Weekly AI research",
        schedule="0 18 * * 5",
        prompt="Research new AI agent releases and send me a summary",
    )
    assert record.next_run_at is not None
    assert record.status == "active"

    listed = await services.scheduler.list()
    assert any(item.id == record.id for item in listed)

    paused = await services.scheduler.set_status(record.id, "paused")
    assert paused is not None and paused.status == "paused"
    assert services.scheduler.next_run_at(record.id) is None

    resumed = await services.scheduler.set_status(record.id, "active")
    assert resumed is not None and resumed.status == "active"
    assert services.scheduler.next_run_at(record.id) is not None

    assert await services.scheduler.delete(record.id) is True
    assert await services.scheduler.get(record.id) is None


@pytest.mark.asyncio
async def test_reminder_is_scheduled_once(services) -> None:
    record = await services.scheduler.create_reminder("Work on Dobot", "2h")
    assert record.kind.value == "reminder"
    assert record.next_run_at is not None
    restored = await services.scheduler.get(record.id)
    assert restored is not None


@pytest.mark.asyncio
async def test_scheduled_task_runs_through_the_same_pipeline(services) -> None:
    """A scheduled run produces a real task with decisions and verification, not a side channel."""
    record = await services.scheduler.create_automation(
        name="Daily greeting", schedule="0 9 * * *", prompt="hello"
    )
    await services.scheduler._fire(record.id, record.prompt)
    tasks = await services.store.all("tasks", limit=50)
    assert any(task.get("source", "").startswith("automation:") for task in tasks)
    refreshed = await services.scheduler.get(record.id)
    assert refreshed is not None and refreshed.run_count == 1
