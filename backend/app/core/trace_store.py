"""Durable, redacted execution traces.

The event bus is intentionally transient. This store is the durable audit trail for a task and is
safe to expose in the desktop UI: secrets are redacted before they reach the record store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.logging_setup import redact
from app.schemas import new_id


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


class TraceStore:
    """Append-only task traces backed by the configured RecordStore."""

    def __init__(self, store: Any, *, retention: int = 1000) -> None:
        self.store = store
        self.retention = retention

    async def append(self, task_id: str, kind: str, **data: Any) -> dict[str, Any]:
        entry = {
            "id": new_id("trace"),
            "task_id": task_id,
            "kind": kind,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": _redact(data),
        }
        await self.store.insert("traces", entry)
        return entry

    async def entries(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        entries = await self.store.all("traces", limit=max(limit * 4, limit))
        selected = [entry for entry in entries if entry.get("task_id") == task_id]
        return selected[-limit:]

    async def prune(self) -> int:
        entries = await self.store.all("traces", limit=100_000)
        if len(entries) <= self.retention:
            return 0
        stale = entries[: len(entries) - self.retention]
        removed = 0
        for entry in stale:
            if await self.store.delete("traces", str(entry.get("id", ""))):
                removed += 1
        return removed
