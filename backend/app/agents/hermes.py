"""Hermes Agent — the execution layer.

Dobot does not reimplement computer use. It orchestrates Hermes, which provides browser automation,
background desktop control (accessibility tree + synthesized input), terminal, filesystem, skills and
scheduling. Three modes:

* ``local``  — Dobot's own guarded tool implementations run in-process; Hermes-specific capabilities
  (desktop control, signed-in browser) report unavailable instead of pretending.
* ``cli``    — an installed Hermes CLI is asked to perform browser/desktop work.
* ``remote`` — a Hermes/MCP HTTP endpoint performs it.

The decision engine always sits in front: the runtime only ever sees work the plan was authorized for.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import Settings, get_settings
from app.core.killswitch import CancellationToken
from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class RuntimeInfo:
    mode: str
    name: str
    available: bool
    detail: str = ""
    toolsets: list[str] = field(default_factory=list)


class ExecutionRuntime(Protocol):
    name: str
    available: bool

    async def info(self) -> RuntimeInfo: ...

    async def computer_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def browser_submit(
        self, *, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]: ...

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class LocalRuntime:
    """No external runtime: tools execute in-process, desktop control is unavailable and says so."""

    name = "dobot-local-tools"
    mode = "local"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.available = True

    async def info(self) -> RuntimeInfo:
        return RuntimeInfo(
            mode=self.mode,
            name=self.name,
            available=True,
            detail="Dobot executes its own guarded tools. Desktop control needs Hermes.",
            toolsets=["filesystem", "terminal", "screen", "web"],
        )

    async def computer_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                f"desktop action '{action}' needs the Hermes runtime "
                "(set AGENT_RUNTIME=cli or remote)"
            ),
        }

    async def browser_submit(
        self, *, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "form submission needs the Hermes browser session (AGENT_RUNTIME=cli or remote)",
        }

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]:
        return {"ok": False, "error": "no external runtime configured", "stdout": ""}

    async def close(self) -> None:
        return None


class HermesCliRuntime:
    """Executes prompts through the Hermes CLI with an explicit toolset allowlist."""

    name = "hermes-cli"
    mode = "cli"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.binary = shutil.which(settings.hermes_command)
        self.available = bool(self.binary)
        self._toolsets = settings.hermes_tools

    async def info(self) -> RuntimeInfo:
        return RuntimeInfo(
            mode=self.mode,
            name=self.name,
            available=self.available,
            detail=(
                f"Hermes CLI at {self.binary} with toolsets {', '.join(self._toolsets)}"
                if self.available
                else f"Hermes CLI '{self.settings.hermes_command}' not found on PATH"
            ),
            toolsets=self._toolsets,
        )

    async def _run(self, args: list[str], timeout: float) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": "hermes CLI not available"}
        try:
            process = await asyncio.create_subprocess_exec(
                str(self.binary),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"ok": False, "error": f"hermes timed out after {timeout:.0f}s"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"hermes invocation failed: {exc}"}
        text_out = stdout.decode("utf-8", errors="replace")
        text_err = stderr.decode("utf-8", errors="replace")
        return {
            "ok": process.returncode == 0,
            "exit_code": process.returncode,
            "stdout": text_out[-8000:],
            "stderr": text_err[-2000:],
            "error": None if process.returncode == 0 else text_err[-500:],
        }

    async def computer_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "Perform exactly this desktop action and report the resulting window state. "
            f"action={action} params={json.dumps(params, ensure_ascii=False)}. "
            "Do not take additional actions."
        )
        result = await self._run(
            [*self.settings.hermes_args.split(), prompt, "-t", "computer_use"],
            timeout=float(self.settings.hermes_timeout_seconds),
        )
        if result.get("ok"):
            result["evidence"] = {"runtime_output": result.get("stdout", "")[-800:]}
            result["expected_window"] = params.get("app") or params.get("target")
        return result

    async def browser_submit(
        self, *, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]:
        prompt = (
            f"Open {url}, fill the form with {json.dumps(fields, ensure_ascii=False)}, "
            f"submit using selector '{submit_selector or 'auto'}', then report the resulting page title "
            "and confirmation text. Do not submit anything else."
        )
        result = await self._run(
            [*self.settings.hermes_args.split(), prompt, "-t", "browser_exec"],
            timeout=float(self.settings.hermes_timeout_seconds),
        )
        if result.get("ok"):
            result["confirmation"] = result.get("stdout", "")[-400:]
            result["page_changed"] = True
        return result

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]:
        tools = ",".join(toolsets or self._toolsets)
        return await self._run(
            [*self.settings.hermes_args.split(), prompt, "-t", tools],
            timeout=float(self.settings.hermes_timeout_seconds),
        )

    async def close(self) -> None:
        return None


class HermesRemoteRuntime:
    """Routes browser/desktop work to a Hermes or MCP HTTP endpoint."""

    name = "hermes-remote"
    mode = "remote"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.available = bool(settings.hermes_endpoint)
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.hermes_endpoint.rstrip("/"),
                timeout=httpx.Timeout(float(self.settings.hermes_timeout_seconds)),
            )
        return self._client

    async def info(self) -> RuntimeInfo:
        return RuntimeInfo(
            mode=self.mode,
            name=self.name,
            available=self.available,
            detail=f"endpoint {self.settings.hermes_endpoint or '(unset)'}",
            toolsets=self.settings.hermes_tools,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "error": "HERMES_ENDPOINT is not configured"}
        try:
            response = await self._http().post(path, json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"hermes remote call failed: {exc}"}

    async def computer_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._post(
            "/computer",
            {"action": action, "params": params, "toolsets": ["computer_use"]},
        )

    async def browser_submit(
        self, *, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]:
        return await self._post(
            "/browser/submit",
            {"url": url, "fields": fields, "submit_selector": submit_selector},
        )

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]:
        return await self._post(
            "/run", {"prompt": prompt, "toolsets": toolsets or self.settings.hermes_tools}
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_runtime(settings: Settings | None = None) -> ExecutionRuntime:
    settings = settings or get_settings()
    mode = settings.agent_runtime
    if mode == "cli":
        runtime = HermesCliRuntime(settings)
        if runtime.available:
            return runtime
        logger.warning("AGENT_RUNTIME=cli but the Hermes CLI was not found; using local tools")
        return LocalRuntime(settings)
    if mode == "remote":
        runtime = HermesRemoteRuntime(settings)
        if runtime.available:
            return runtime
        logger.warning("AGENT_RUNTIME=remote but HERMES_ENDPOINT is unset; using local tools")
        return LocalRuntime(settings)
    return LocalRuntime(settings)


async def plan_prompt(
    plan_steps: list[dict[str, Any]],
    *,
    token: CancellationToken | None = None,
) -> str:
    """Render an authorized plan into an instruction envelope for the runtime.

    The envelope is the *only* thing a runtime may act on: it names approved steps explicitly, so a
    runtime cannot invent additional work.
    """
    lines = [
        "You are the execution layer for Dobot. Perform ONLY these approved steps, in order.",
        "If any step falls outside what you can do safely, stop and report it.",
        "",
    ]
    for index, step in enumerate(plan_steps, start=1):
        lines.append(f"{index}. tool={step.get('tool')} params={json.dumps(step.get('params', {}), ensure_ascii=False)}")
        if step.get("expected"):
            lines.append(f"   expected: {step['expected']}")
    if token:
        lines.append("")
        lines.append("The user can cancel at any time; abort immediately if told to stop.")
    return "\n".join(lines)
