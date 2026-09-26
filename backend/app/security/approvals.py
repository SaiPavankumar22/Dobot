"""Approval centre.

Sensitive actions stop here. The orchestrator creates an approval, emits ``approval_required``, and
awaits the user's decision — which may arrive seconds later from the desktop dialog, or never (in
which case the task parks in WAITING_APPROVAL and can be resumed later).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger
from app.memory.store import RecordStore
from app.schemas import (
    ActionSpec,
    ApprovalRecord,
    ApprovalStatus,
    RiskLevel,
)

logger = get_logger(__name__)


class ApprovalStore:
    def __init__(self, store: RecordStore, bus: EventBus | None = None) -> None:
        self._store = store
        self._bus = bus or get_event_bus()
        self._records: dict[str, ApprovalRecord] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        task_id: str,
        step_id: str,
        action: ActionSpec,
        risk: RiskLevel,
        description: str = "",
        preview: list[str] | None = None,
        reasons: list[str] | None = None,
    ) -> ApprovalRecord:
        payload = {
            "tool": action.tool,
            "params": action.params,
            "expected": action.expected,
            "reasons": reasons or [],
        }
        record = ApprovalRecord(
            task_id=task_id,
            step_id=step_id,
            action=action.tool,
            risk=risk,
            description=description or action.description or f"{action.tool} requires confirmation",
            payload=payload,
            preview=preview or [],
        )
        async with self._lock:
            self._records[record.id] = record
            self._events[record.id] = asyncio.Event()
        await self._store.insert("approvals", record.model_dump(mode="json"))
        await self._bus.emit(
            EventType.APPROVAL_REQUIRED,
            message=record.description,
            task_id=task_id,
            approval=record.model_dump(mode="json"),
        )
        logger.info("approval %s requested for %s (%s)", record.id, action.tool, risk.value)
        return record

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        if approval_id in self._records:
            return self._records[approval_id]
        doc = await self._store.get("approvals", approval_id)
        if not doc:
            return None
        record = ApprovalRecord.model_validate(doc)
        self._records[approval_id] = record
        self._events.setdefault(approval_id, asyncio.Event())
        return record

    async def pending(self) -> list[ApprovalRecord]:
        docs = await self._store.all("approvals", limit=500)
        records: list[ApprovalRecord] = []
        for doc in docs:
            try:
                record = ApprovalRecord.model_validate(doc)
            except Exception:  # noqa: BLE001
                continue
            if record.status is ApprovalStatus.PENDING:
                records.append(record)
                self._records.setdefault(record.id, record)
                self._events.setdefault(record.id, asyncio.Event())
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    async def history(self, limit: int = 100) -> list[ApprovalRecord]:
        docs = await self._store.all("approvals", limit=limit)
        records: list[ApprovalRecord] = []
        for doc in docs:
            try:
                records.append(ApprovalRecord.model_validate(doc))
            except Exception:  # noqa: BLE001
                continue
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records[:limit]

    async def resolve(
        self,
        approval_id: str,
        decision: str,
        *,
        note: str = "",
        edits: dict | None = None,
    ) -> ApprovalRecord | None:
        record = await self.get(approval_id)
        if record is None:
            return None
        if record.status is not ApprovalStatus.PENDING:
            return record

        status = {
            "approve": ApprovalStatus.APPROVED,
            "reject": ApprovalStatus.REJECTED,
            "edit": ApprovalStatus.EDITED,
        }.get(decision, ApprovalStatus.REJECTED)

        record.status = status
        record.resolved_at = datetime.now(UTC)
        record.decision_note = note
        if edits:
            record.payload = {**record.payload, "edits": edits}

        self._records[approval_id] = record
        await self._store.update(
            "approvals",
            approval_id,
            {
                "status": record.status.value,
                "resolved_at": record.resolved_at.isoformat(),
                "decision_note": note,
                "payload": record.payload,
            },
        )
        event = self._events.setdefault(approval_id, asyncio.Event())
        event.set()
        await self._bus.emit(
            EventType.APPROVAL_RESOLVED,
            message=f"{record.action}: {status.value.lower()}",
            task_id=record.task_id,
            approval_id=approval_id,
            decision=status.value,
        )
        return record

    async def wait_for_decision(self, approval_id: str, timeout: float | None = None) -> ApprovalRecord | None:
        """Block until the user decides. Returns the resolved record, or None on timeout."""
        event = self._events.setdefault(approval_id, asyncio.Event())
        if not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError:
                logger.info("approval %s is still waiting for a decision", approval_id)
                return None
        return await self.get(approval_id)

    def expired_pending(self) -> list[str]:
        return [
            approval_id
            for approval_id, record in self._records.items()
            if record.status is ApprovalStatus.PENDING
        ]
