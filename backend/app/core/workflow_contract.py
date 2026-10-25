"""Versioned workflow metadata for scheduled and reusable automations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkflowContract:
    """A safe, resumable workflow envelope around an existing automation prompt."""

    version: int = 1
    name: str = ""
    owner_id: str = "local"
    approval_required: bool = True
    idempotency_key: str = ""
    timeout_seconds: int = 900
    max_retries: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "owner_id": self.owner_id,
            "approval_required": self.approval_required,
            "idempotency_key": self.idempotency_key,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "metadata": dict(self.metadata),
        }
