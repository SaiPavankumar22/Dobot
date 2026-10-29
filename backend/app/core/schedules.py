"""Schedule parsing: cron expressions, natural phrases, intervals and one-shots.

Adapted from NousResearch/hermes-agent ``cron/jobs.py::parse_schedule`` (MIT). Hermes stores a
schedule as a dict (``kind`` + payload); Dobot keeps the user's text in ``AutomationRecord.schedule``
and re-parses it whenever a job is (re)armed, so every form here has to be **idempotent** — parsing
the same string twice must yield the same instant. That rules out Hermes' bare ``"in 30m"`` as a
stored value, so callers normalise one-shots to an ISO timestamp before persisting (see
``Scheduler.create_automation``).

Accepted inputs:

===============  =========================================================
``0 9 * * *``    five-field cron (validated by ``CronTrigger``)
``every monday 9am`` / ``weekdays at 9am`` / ``every day at 8pm`` → cron
``every 30m`` / ``every 2h`` / ``30m``  recurring interval
``in 30m``       one-shot, resolved to a timestamp by the caller
ISO timestamp    one-shot
``tomorrow at 8pm``  one-shot (via the reminder parser)
===============  =========================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from apscheduler.triggers.cron import CronTrigger

_FIELD_COUNT = 5
#: `"30m"`, `"2h"`, `"1d"`, and Hermes' bare-unit form (`"hour"` → 1 hour).
_DURATION = re.compile(r"^(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.I)
_MINUTES_PER_UNIT = {"m": 1, "h": 60, "d": 1440}
#: One cron field. The '-' must sit at the end of the class: written as `\*-` it would be read as
#: a range from '*' to '-' and silently drop the '-' itself, rejecting `1-5` (Hermes' copy has this).
_CRON_TOKEN = re.compile(r"^[A-Za-z0-9*,/-]+$")

#: Catch-up window: half a period, clamped to 2 minutes – 2 hours (Hermes' `grace_minutes` rule;
#: 0 would mean "never catch up", which Dobot does not expose).
GRACE_FLOOR_SECONDS = 120
GRACE_CEILING_SECONDS = 7200

_WEEKDAY_TO_CRON_DOW = {
    "sunday": "0", "sun": "0",
    "monday": "1", "mon": "1",
    "tuesday": "2", "tue": "2", "tues": "2",
    "wednesday": "3", "wed": "3", "weds": "3",
    "thursday": "4", "thu": "4", "thur": "4", "thurs": "4",
    "friday": "5", "fri": "5",
    "saturday": "6", "sat": "6",
}
_DAYSPEC_TO_CRON_DOW = {
    "day": "*", "daily": "*", "everyday": "*",
    "weekday": "1-5", "weekdays": "1-5",
    "weekend": "0,6", "weekends": "0,6",
}

HELP = (
    "Use a cron expression ('0 9 * * *'), a natural schedule ('every monday 9am', "
    "'weekdays at 9am', 'every day at 8pm'), a recurring interval ('every 30m', 'every 2h') "
    "or a one-shot ('in 30m', '2026-11-03T09:00:00')."
)


@dataclass(frozen=True)
class ParsedSchedule:
    """What the scheduler can arm: a cron expression, an interval, or a single instant."""

    kind: Literal["cron", "interval", "once"]
    display: str = ""
    cron: str = ""
    minutes: int = 0
    run_at: datetime | None = None

    @property
    def recurring(self) -> bool:
        return self.kind in ("cron", "interval")


def validate_cron(expression: str) -> str:
    """Return a five-field cron expression or raise ``ValueError``."""
    fields = expression.split()
    if len(fields) != _FIELD_COUNT or not all(_CRON_TOKEN.match(field) for field in fields):
        raise ValueError(f"cron expression needs {_FIELD_COUNT} fields, got {len(fields)}")
    CronTrigger.from_crontab(expression)  # raises on invalid syntax
    return expression


def parse_duration_minutes(text: str) -> int:
    """``"30m"`` → 30, ``"2h"`` → 120, ``"1d"`` → 1440, ``"hour"`` → 60."""
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise ValueError(f"cannot read duration '{text}' — use 30m, 2h or 1d")
    amount = int(match.group(1)) if match.group(1) else 1
    return amount * _MINUTES_PER_UNIT[match.group(2)[0].lower()]


def parse_clock_time(text: str) -> tuple[int, int] | None:
    """``9am`` / ``9:30am`` / ``14:00`` / ``noon`` / ``midnight`` → ``(hour, minute)``."""
    value = text.strip().lower().replace(" ", "")
    if not value:
        return None
    if value in ("noon", "midday"):
        return (12, 0)
    if value == "midnight":
        return (0, 0)
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return (hour, minute)


def natural_to_cron(rest: str) -> str | None:
    """``monday 9am`` / ``weekdays at 9am`` / ``every day at 8pm`` → 5-field cron, else None."""
    tokens = rest.lower().replace(",", " ").split()
    if not tokens:
        return None
    dow = _DAYSPEC_TO_CRON_DOW.get(tokens[0])
    index = 1
    if dow is None:
        days: list[str] = []
        index = len(tokens)
        for position, token in enumerate(tokens):
            if token == "and":
                continue
            mapped = _WEEKDAY_TO_CRON_DOW.get(token)
            if mapped is None:
                index = position
                break
            if mapped not in days:
                days.append(mapped)
        if not days:
            return None
        dow = ",".join(days)
    time_tokens = tokens[index:]
    if time_tokens and time_tokens[0] == "at":
        time_tokens = time_tokens[1:]
    if not time_tokens:
        return None
    clock = parse_clock_time(" ".join(time_tokens))
    if clock is None:
        return None
    hour, minute = clock
    return f"{minute} {hour} * * {dow}"


def parse_schedule(value: str, *, now: datetime | None = None) -> ParsedSchedule:
    """Parse any accepted schedule text. Raises ``ValueError`` with ``HELP`` when unreadable."""
    from app.core.scheduler import parse_when  # local import: scheduler re-exports this module

    text = value.strip()
    if not text:
        raise ValueError(f"empty schedule. {HELP}")
    lowered = text.lower()
    now = now or datetime.now()

    is_every = lowered.startswith("every ")
    rest = text[6:].strip() if is_every else lowered

    cron_expression = natural_to_cron(rest)
    if cron_expression:
        validate_cron(cron_expression)
        return ParsedSchedule(kind="cron", display=text, cron=cron_expression)

    if is_every:
        minutes = parse_duration_minutes(rest)
        return ParsedSchedule(kind="interval", display=f"every {minutes}m", minutes=minutes)

    parts = text.split()
    if len(parts) == _FIELD_COUNT and all(_CRON_TOKEN.match(part) for part in parts):
        validate_cron(text)
        return ParsedSchedule(kind="cron", display=text, cron=text)

    if lowered.startswith("in "):
        delta = timedelta(minutes=parse_duration_minutes(text[3:].strip()))
        return ParsedSchedule(kind="once", display=text, run_at=now + delta)

    if "T" in text or re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"cannot read timestamp '{text}': {exc}") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return ParsedSchedule(kind="once", display=f"once at {parsed:%Y-%m-%d %H:%M}", run_at=parsed)

    if _DURATION.match(lowered):
        minutes = parse_duration_minutes(lowered)
        return ParsedSchedule(kind="interval", display=f"every {minutes}m", minutes=minutes)

    # Last resort: the reminder phrasing Dobot already understands ("tomorrow at 8pm").
    try:
        moment = parse_when(text, now=now)
    except ValueError:
        raise ValueError(f"cannot interpret schedule '{text}'. {HELP}") from None
    return ParsedSchedule(kind="once", display=f"once at {moment:%Y-%m-%d %H:%M}", run_at=moment)


def catch_up_grace_seconds(cadence_seconds: float | None) -> int:
    """Half the period, clamped to 2 minutes – 2 hours (Hermes' catch-up window)."""
    if not cadence_seconds or cadence_seconds <= 0:
        return GRACE_CEILING_SECONDS
    half = int(cadence_seconds / 2)
    return max(GRACE_FLOOR_SECONDS, min(GRACE_CEILING_SECONDS, half))
