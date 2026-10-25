"""Best-effort supervision for operating-system child processes.

The asyncio kill switch cannot terminate a subprocess that a tool spawned. This small platform
adapter keeps that cleanup concern in one place and reports whether termination was graceful or
forced. Tools can opt in by registering the PID they create.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass


@dataclass
class ProcessHandle:
    task_id: str
    pid: int
    name: str = ""


class ProcessSupervisor:
    def __init__(self) -> None:
        self._handles: dict[str, list[ProcessHandle]] = {}

    def register(self, task_id: str, pid: int, name: str = "") -> ProcessHandle:
        handle = ProcessHandle(task_id=task_id, pid=int(pid), name=name)
        self._handles.setdefault(task_id, []).append(handle)
        return handle

    def handles(self, task_id: str | None = None) -> list[ProcessHandle]:
        if task_id is None:
            return [handle for values in self._handles.values() for handle in values]
        return list(self._handles.get(task_id, []))

    async def terminate(self, task_id: str, *, force: bool = False) -> dict[str, int | bool]:
        handles = self._handles.pop(task_id, [])
        terminated = 0
        forced = 0
        for handle in handles:
            if not _alive(handle.pid):
                continue
            if os.name == "nt":
                command = ["taskkill", "/PID", str(handle.pid), "/T", "/F" if force else "/T"]
                if not force:
                    command = ["taskkill", "/PID", str(handle.pid), "/T"]
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await process.wait()
                if process.returncode == 0:
                    terminated += 1
                    forced += int(force)
            else:
                try:
                    os.kill(handle.pid, signal.SIGKILL if force else signal.SIGTERM)
                    terminated += 1
                    forced += int(force)
                except ProcessLookupError:
                    pass
        return {"terminated": terminated, "forced": forced, "partial": terminated < len(handles)}

    def release(self, task_id: str) -> None:
        self._handles.pop(task_id, None)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
