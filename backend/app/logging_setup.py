"""Structured logging with secret redaction.

Logs must never contain API keys, tokens, or captured screen content. ``redact`` is applied to every
record, and the configured secret values are learned from settings at startup.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from app.config import get_settings

_REDACTED = "***redacted***"
_secrets: list[str] = []
_MAX_PREVIEW = 400


def register_secrets(values: list[str]) -> None:
    """Teach the redactor about secret values (called once at startup)."""
    global _secrets
    _secrets = [value for value in values if value and len(value) > 3]


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(value) if isinstance(value, str) else value for value in record.args
                )
        return True


class DevFormatter(logging.Formatter):
    """Compact `[HH:MM:SS] LEVEL logger — message` lines for the console."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        base = f"[{stamp}] {record.levelname:<7} {record.name} — {redact(record.getMessage())}"
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for the activity log and future log shipping."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage())[:_MAX_PREVIEW],
        }
        for key in ("task_id", "tool", "event_type"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))[:_MAX_PREVIEW]
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    settings = get_settings()
    register_secrets(settings.secret_values)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.dobot_env == "production" else DevFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Third-party chatter that would drown out the agent timeline.
    for noisy in ("httpx", "httpcore", "apscheduler", "uvicorn.access", "motor"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
