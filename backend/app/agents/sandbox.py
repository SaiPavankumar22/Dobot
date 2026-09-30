"""The execution boundary: NemoClaw / OpenShell.

Everything Hermes executes passes through here. Two backends implement one interface:

* ``NemoClawSandbox`` — the real boundary. Lifecycle and status come from the NemoClaw host CLI, and
  command execution goes through the OpenShell gateway when it is configured. Credentials stay
  outside the sandbox: the gateway substitutes them at approved egress.
* ``LocalSandbox`` — Dobot's own action firewall. Same path and host allowlist checks run in-process;
  there is **no kernel-level isolation**. The dashboard's Security page reports which one is live
  instead of implying safety Dobot does not have.

NemoClaw's tested platforms are Linux/DGX Spark (Windows through WSL), so on a plain Windows host the
honest state is ``isolation: none`` until the user runs the NemoClaw stack.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.config import Settings, get_settings
from app.core.killswitch import CancellationToken
from app.logging_setup import get_logger
from app.security.policies import is_forbidden_write, is_protected_name

logger = get_logger(__name__)


class SandboxDenied(Exception):
    """A path, host or command refused by the boundary."""


class SandboxUnavailable(Exception):
    """The configured sandbox backend cannot serve this request."""


@dataclass
class CommandResult:
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    backend: str = "local"


@dataclass
class SandboxStatus:
    provider: str = "local"
    isolation: str = "none"
    available: bool = True
    degraded: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class Sandbox(Protocol):
    name: str

    async def check_path(self, raw: str, *, write: bool = False) -> Path: ...

    async def check_host(self, host: str) -> str: ...

    async def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 120.0,
        token: CancellationToken | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...

    async def status(self) -> SandboxStatus: ...



class LocalSandbox:
    """Action firewall: allowlisted roots, allowlisted hosts, hard timeouts, no credentials."""

    name = "action-firewall"

    def __init__(self, settings: Settings, grants: Any = None) -> None:
        self.settings = settings
        self.allowed_paths = list(settings.allowed_paths) or [Path.home().resolve()]
        self.allowed_hosts = settings.allowed_hosts
        #: Remembered user permissions (GrantStore). A path outside the roots is still refused
        #: unless the user granted that scope — and even a grant never unlocks a system or
        #: credential location, which this firewall checks independently.
        self.grants = grants

    async def check_path(self, raw: str, *, write: bool = False) -> Path:
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise SandboxDenied(f"cannot resolve path {raw}: {exc}") from exc
        if any(_is_inside(resolved, root) for root in self.allowed_paths):
            return resolved
        # Outside the workspace: only an explicit, remembered permission can open this door, and
        # only for folders the user approved — never for system directories or credential files.
        if write and is_forbidden_write(resolved):
            raise SandboxDenied(f"{resolved} is a system location and is never writable")
        if is_protected_name(resolved.name):
            raise SandboxDenied(f"{resolved.name} is a protected credential file")
        if self.grants is not None and self.grants.allows_path(resolved):
            return resolved
        raise SandboxDenied(
            f"{resolved} is outside the allowed roots "
            f"({', '.join(str(root) for root in self.allowed_paths)})"
        )

    async def check_host(self, host: str) -> str:
        host = (host or "").lower().strip()
        if not host:
            raise SandboxDenied("empty host")
        if not self.allowed_hosts:
            return host
        if host in self.allowed_hosts:
            return host
        if any(host.endswith(f".{allowed}") for allowed in self.allowed_hosts):
            return host
        raise SandboxDenied(f"network egress to {host} is not on the allowlist")

    async def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 120.0,
        token: CancellationToken | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if token:
            token.raise_if_cancelled()
        if cwd:
            await self.check_path(cwd)
        started = time.perf_counter()
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**_safe_env(), **(env or {})},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return CommandResult(
                ok=False,
                exit_code=-9,
                stderr=f"command timed out after {timeout:.0f}s",
                duration_ms=int((time.perf_counter() - started) * 1000),
                backend=self.name,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        duration = int((time.perf_counter() - started) * 1000)
        return CommandResult(
            ok=process.returncode == 0,
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace")[-20000:],
            stderr=stderr.decode("utf-8", errors="replace")[-8000:],
            duration_ms=duration,
            backend=self.name,
        )

    async def status(self) -> SandboxStatus:
        return SandboxStatus(
            provider="local",
            isolation="none",
            available=True,
            degraded=False,
            details={
                "allowed_paths": [str(path) for path in self.allowed_paths],
                "allowed_hosts": self.allowed_hosts,
                "note": "In-process action firewall. No kernel isolation — configure NemoClaw for a real sandbox.",
            },
        )


class NemoClawSandbox:
    """Routes execution through the NemoClaw-managed OpenShell sandbox."""

    name = "nemoclaw"

    def __init__(self, settings: Settings, local: LocalSandbox) -> None:
        self.settings = settings
        self.local = local
        self._cli = shutil.which(settings.nemoclaw_command)
        self._client: httpx.AsyncClient | None = None

    def _gateway(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.openshield_gateway_url.rstrip("/"),
                timeout=httpx.Timeout(600.0),
            )
        return self._client

    async def check_path(self, raw: str, *, write: bool = False) -> Path:
        return await self.local.check_path(raw, write=write)

    async def check_host(self, host: str) -> str:
        return await self.local.check_host(host)

    async def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 120.0,
        token: CancellationToken | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if not self.settings.openshield_gateway_url:
            raise SandboxUnavailable(
                "OpenShell gateway URL is not configured; cannot execute inside NemoClaw"
            )
        if token:
            token.raise_if_cancelled()
        started = time.perf_counter()
        response = await self._gateway().post(
            "/exec",
            json={
                "sandbox": self.settings.nemoclaw_sandbox,
                "command": command,
                "cwd": cwd,
                "timeout_seconds": timeout,
                "network_policy": self.settings.allowed_hosts,
            },
        )
        duration = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise SandboxUnavailable(
                f"OpenShell gateway rejected execution ({response.status_code}): {response.text[:300]}"
            )
        payload: dict[str, Any] = response.json()
        return CommandResult(
            ok=bool(payload.get("ok", payload.get("exit_code", 1) == 0)),
            exit_code=int(payload.get("exit_code", 0)),
            stdout=str(payload.get("stdout", ""))[-20000:],
            stderr=str(payload.get("stderr", ""))[-8000:],
            duration_ms=int(payload.get("duration_ms", duration)),
            backend=self.name,
        )

    async def probe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "cli": self._cli or "",
            "sandbox": self.settings.nemoclaw_sandbox,
            "gateway": bool(self.settings.openshield_gateway_url),
            "running": False,
        }
        if not self._cli:
            info["reason"] = "nemoclaw CLI not found on PATH"
            return info
        try:
            process = await asyncio.create_subprocess_exec(
                self._cli,
                "status",
                "--sandbox",
                self.settings.nemoclaw_sandbox,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
            info["running"] = process.returncode == 0
            info["output"] = (stdout or stderr).decode("utf-8", errors="replace")[:800]
        except Exception as exc:  # noqa: BLE001
            info["reason"] = str(exc)
        return info

    async def status(self) -> SandboxStatus:
        probe = await self.probe()
        usable = bool(probe.get("running")) and bool(probe.get("gateway"))
        return SandboxStatus(
            provider="nemoclaw",
            isolation="openshell" if usable else "none",
            available=usable,
            degraded=not usable,
            details=probe,
        )


class FallbackSandbox:
    """Prefers NemoClaw, degrades to the action firewall, and always tells the truth about which."""

    def __init__(self, primary: Sandbox, fallback: Sandbox, settings: Settings) -> None:
        self._primary = primary
        self._fallback = fallback
        self._settings = settings
        self._degraded = settings.sandbox_provider != "nemoclaw"

    @property
    def name(self) -> str:
        return self._primary.name if not self._degraded else self._fallback.name

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _active(self) -> Sandbox:
        return self._fallback if self._degraded else self._primary

    async def check_path(self, raw: str, *, write: bool = False) -> Path:
        return await self._active().check_path(raw, write=write)

    async def check_host(self, host: str) -> str:
        return await self._active().check_host(host)

    async def run_command(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float = 120.0,
        token: CancellationToken | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if not self._degraded:
            try:
                return await self._primary.run_command(
                    command, cwd=cwd, timeout=timeout, token=token, env=env
                )
            except SandboxUnavailable as exc:
                logger.warning("nemoclaw unavailable (%s); using action firewall", exc)
                self._degraded = True
        return await self._fallback.run_command(
            command, cwd=cwd, timeout=timeout, token=token, env=env
        )

    async def status(self) -> SandboxStatus:
        status = await self._active().status()
        status.degraded = self._degraded
        if self._degraded and status.provider != "local":
            status.isolation = "none"
        return status

    async def close(self) -> None:
        for sandbox in (self._primary, self._fallback):
            client = getattr(sandbox, "_client", None)
            if client is not None:
                await client.aclose()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_env() -> dict[str, str]:
    """A minimal environment: PATH and locale only. No secrets reach a subprocess by default."""
    import os

    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "HOME")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    # Keep the allowlisted tooling usable without handing over tokens.
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def build_sandbox(settings: Settings | None = None, grants: Any = None) -> FallbackSandbox:
    settings = settings or get_settings()
    local = LocalSandbox(settings, grants=grants)
    if settings.sandbox_provider == "nemoclaw":
        return FallbackSandbox(NemoClawSandbox(settings, local), local, settings)
    return FallbackSandbox(local, local, settings)


def host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()
