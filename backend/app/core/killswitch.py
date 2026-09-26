"""Kill switch.

A user pressing Ctrl+Shift+Esc (or clicking STOP) must stop real work immediately — not after the
current step finishes. Every execution runs under a token; long-running tools check it between
operations, and the switch also cancels the asyncio task driving the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.logging_setup import get_logger

logger = get_logger(__name__)


class TaskCancelled(Exception):
    """Raised inside a tool when the user has killed the active task."""


@dataclass
class CancellationToken:
    task_id: str
    _cancelled: bool = False
    _reason: str = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "stopped by user") -> None:
        self._cancelled = True
        self._reason = reason

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise TaskCancelled(self._reason)


@dataclass
class KillSwitch:
    _tokens: dict[str, CancellationToken] = field(default_factory=dict)
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def register(self, task_id: str, asyncio_task: asyncio.Task | None = None) -> CancellationToken:
        token = CancellationToken(task_id=task_id)
        self._tokens[task_id] = token
        if asyncio_task is not None:
            self._tasks[task_id] = asyncio_task
        return token

    def token(self, task_id: str) -> CancellationToken | None:
        return self._tokens.get(task_id)

    def is_active(self, task_id: str) -> bool:
        return task_id in self._tokens

    @property
    def active_ids(self) -> list[str]:
        return sorted(self._tokens)

    def release(self, task_id: str) -> None:
        self._tokens.pop(task_id, None)
        self._tasks.pop(task_id, None)

    def kill(self, task_id: str | None = None, reason: str = "stopped by user") -> list[str]:
        """Cancel one task, or every active task when ``task_id`` is None."""
        targets = [task_id] if task_id else list(self._tokens)
        killed: list[str] = []
        for target in targets:
            if target is None:
                continue
            token = self._tokens.get(target)
            if token:
                token.cancel(reason)
            running = self._tasks.get(target)
            if running and not running.done():
                running.cancel()
            killed.append(target)
            logger.info("kill switch fired for %s (%s)", target, reason)
        for target in killed:
            self.release(target)
        return killed


_switch: KillSwitch | None = None


def get_kill_switch() -> KillSwitch:
    global _switch
    if _switch is None:
        _switch = KillSwitch()
    return _switch


def reset_kill_switch() -> None:
    """Test helper."""
    global _switch
    _switch = KillSwitch()
