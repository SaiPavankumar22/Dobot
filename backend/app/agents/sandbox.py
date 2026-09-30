"""The execution boundary: NemoClaw / OpenShell / Nebius sandboxes.

Everything Hermes executes passes through here. Three backends implement one interface:

* ``NemoClawSandbox`` — the real boundary. Lifecycle and status come from the NemoClaw host CLI, and
  command execution goes through the OpenShell gateway when it is configured. Credentials stay
  outside the sandbox: the gateway substitutes them at approved egress.
* ``NebiusSandbox`` — execution inside a Nebius Sandboxes (ConTree beta) VM: VM-level isolation,
  a persistent versioned filesystem per session, and nothing running on this machine at all.
  Configured with ``SANDBOX_PROVIDER=nebius`` plus ``NEBIUS_API_KEY`` / ``NEBIUS_PROJECT_ID``.
* ``LocalSandbox`` — Dobot's own action firewall. Same path and host allowlist checks run in-process;
  there is **no kernel-level isolation**. The dashboard's Security page reports which one is live
  instead of implying safety Dobot does not have.

NemoClaw's tested platforms are Linux/DGX Spark (Windows through WSL), so on a plain Windows host the
honest state is ``isolation: none`` until the user runs the NemoClaw stack — which is exactly when
the Nebius option becomes interesting: it needs no local daemon at all.
"""

from __future__ import annotations

import asyncio
import shlex
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


class NebiusSandbox:
    """Remote execution inside a Nebius Sandboxes (ConTree) VM — the agent's own computer.

    Every command runs in a cloud container with VM-level isolation instead of on this machine,
    and the session is versioned: files one command leaves behind are still there for the next
    one, and a timed-out branch never corrupts live state (ConTree checkpoints are Git-like).
    Path and host policy still run locally — ``check_path``/``check_host`` delegate to the action
    firewall, so file tools keep operating on the real workspace while ``terminal_run`` executes
    remotely, exactly the split NemoClaw already established.

    Credentials come from Settings (``NEBIUS_API_KEY`` + ``NEBIUS_PROJECT_ID``), never from the
    process environment: the backend reads ``.env`` itself. Any configuration or transport failure
    raises ``SandboxUnavailable`` so ``FallbackSandbox`` degrades to the action firewall and the
    dashboard reports the honest isolation state.
    """

    name = "nebius"
    #: A reachability probe is cached this long (seconds) so the Security page never hammers the API.
    probe_ttl = 60.0

    def __init__(self, settings: Settings, local: LocalSandbox) -> None:
        self.settings = settings
        self.local = local
        self._client: Any = None
        #: Latest sandbox image state; each successful command chains the next one from it.
        self._state: Any = None
        self._lock = asyncio.Lock()
        self._probe_cache: dict[str, Any] | None = None
        self._probe_at = 0.0

    # -- connection ------------------------------------------------------------

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        key = self.settings.nebius_api_key.strip()
        project = self.settings.nebius_project_id.strip()
        if not key:
            raise SandboxUnavailable("NEBIUS_API_KEY is not configured; the Nebius sandbox cannot authenticate")
        if not project:
            raise SandboxUnavailable("NEBIUS_PROJECT_ID is not configured; the Nebius sandbox has no project to run in")
        try:
            from contree_sdk import ContreeSync
            from contree_sdk.auth import IAMAuth
            from contree_sdk.config import ContreeConfig
        except ImportError as exc:
            raise SandboxUnavailable("contree-sdk is not installed (pip install contree-sdk)") from exc
        auth = IAMAuth(
            token=key,
            project_id=project,
            base_url=self.settings.nebius_sandbox_base_url,
        )
        self._client = ContreeSync(ContreeConfig(auth=auth))
        return self._client

    @staticmethod
    def _shell_for(command: str, cwd: str | None) -> str:
        """Wrap a command so the remote cwd exists — the sandbox filesystem starts fresh."""
        if not cwd:
            return command
        quoted = shlex.quote(str(cwd))
        return f"mkdir -p {quoted} && cd {quoted} && {{ {command} }}"

    def _execute(self, command: str, cwd: str | None, timeout: float, env: dict[str, str] | None) -> Any:
        """Blocking SDK call — always runs inside ``asyncio.to_thread``."""
        client = self._connect()
        image = self._state
        if image is None:
            image = client.images.use(self.settings.nebius_sandbox_image)
        # disposable=False is the whole persistence mechanism: a disposable run is one-shot
        # (chaining the next command raises DisposableImageRunError), while a kept instance
        # versions the filesystem so files survive into the next command's checkpoint.
        operation = image.run(
            shell=self._shell_for(command, cwd),
            env=env or None,
            timeout=timeout,
            disposable=False,
        )
        return operation.wait()

    def _probe_sync(self) -> dict[str, Any]:
        """Cheapest authenticated call we have: list the account's sandbox images."""
        client = self._connect()
        images = client.images()
        count = len(images) if hasattr(images, "__len__") else -1
        return {"reachable": True, "images": count}

    # -- Sandbox protocol ------------------------------------------------------

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
        if token:
            token.raise_if_cancelled()
        started = time.perf_counter()
        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._execute, command, cwd, timeout, env),
                    # The SDK polls its own operations; give it the sandbox timeout plus a
                    # margin for image pull and transport before declaring the run lost.
                    timeout=timeout + 30.0,
                )
            except TimeoutError:
                return CommandResult(
                    ok=False,
                    exit_code=-9,
                    stderr=f"nebius sandbox command timed out after {timeout:.0f}s",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    backend=self.name,
                )
            except SandboxUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - transport/SDK failures become one honest error
                raise SandboxUnavailable(f"nebius sandbox unreachable: {exc}") from exc
            self._state = result
        duration = int((time.perf_counter() - started) * 1000)
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        return CommandResult(
            ok=exit_code == 0,
            exit_code=exit_code,
            stdout=str(getattr(result, "stdout", "") or "")[-20000:],
            stderr=str(getattr(result, "stderr", "") or "")[-8000:],
            duration_ms=duration,
            backend=self.name,
        )

    async def status(self) -> SandboxStatus:
        now = time.monotonic()
        if self._probe_cache is None or now - self._probe_at > self.probe_ttl:
            try:
                details = await asyncio.wait_for(asyncio.to_thread(self._probe_sync), timeout=10.0)
            except TimeoutError:
                details = {"reachable": False, "reason": "probe timed out after 10s"}
            except SandboxUnavailable as exc:
                details = {"reachable": False, "reason": str(exc)}
            except Exception as exc:  # noqa: BLE001
                details = {"reachable": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
            self._probe_cache = details
            self._probe_at = now
        details = dict(self._probe_cache)
        details["image"] = self.settings.nebius_sandbox_image
        usable = bool(details.get("reachable"))
        return SandboxStatus(
            provider="nebius",
            isolation="vm" if usable else "none",
            available=usable,
            degraded=not usable,
            details=details,
        )


class FallbackSandbox:
    """Prefers NemoClaw, degrades to the action firewall, and always tells the truth about which."""

    def __init__(self, primary: Sandbox, fallback: Sandbox, settings: Settings) -> None:
        self._primary = primary
        self._fallback = fallback
        self._settings = settings
        # "local" as the requested provider means the user never asked for a remote sandbox;
        # nemoclaw and nebius both start healthy and only degrade if they fail at runtime.
        self._degraded = settings.sandbox_provider not in ("nemoclaw", "nebius")

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
                logger.warning("%s unavailable (%s); using action firewall", self._primary.name, exc)
                self._degraded = True
        return await self._fallback.run_command(
            command, cwd=cwd, timeout=timeout, token=token, env=env
        )

    async def status(self) -> SandboxStatus:
        status = await self._active().status()
        # A provider that is configured but unreachable is degraded too, even before the first
        # command tried to run — the Security page must never show a healthy flag for it.
        status.degraded = self._degraded or not status.available
        if self._degraded and status.provider != "local":
            status.isolation = "none"
        return status

    async def close(self) -> None:
        for sandbox in (self._primary, self._fallback):
            client = getattr(sandbox, "_client", None)
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                await aclose()


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
    if settings.sandbox_provider == "nebius":
        return FallbackSandbox(NebiusSandbox(settings, local), local, settings)
    return FallbackSandbox(local, local, settings)


def host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()
