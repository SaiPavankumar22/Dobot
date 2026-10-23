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
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.agents.sandbox import _safe_env
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
                # The CLI's documented config lives in %LOCALAPPDATA%\hermes\.env (LOCALAPPDATA
                # is kept), but a user may export HERMES_* / AGENT_RUNTIME as overrides — pass
                # exactly those through while unrelated shell-exported keys stay out of the child.
                env={
                    **_safe_env(),
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key.startswith("HERMES_") or key == "AGENT_RUNTIME"
                    },
                },
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


class HermesApiRuntime:
    """Drives the real Hermes Agent gateway (NousResearch/hermes-agent) over HTTP.

    The contract is taken from the upstream MIT-licensed source,
    ``gateway/platforms/api_server.py`` + ``gateway/platforms/api_server_runs.py``:

    * ``GET  /v1/health``                 liveness (no auth on loopback)
    * ``GET  /v1/models``, ``/v1/capabilities``, ``/v1/toolsets``   discovery
    * ``POST /v1/runs`` ``{"input", "session_id"}`` -> ``{"run_id"}``
    * ``GET  /v1/runs/{run_id}/events``   SSE: ``tool.started`` / ``tool.completed`` /
      ``run.completed`` / ``run.failed`` / ``run.cancelled``
    * ``POST /v1/runs/{run_id}/stop``     cancellation
    * ``GET  /v1/runs/{run_id}``          pollable status (``output``, ``error``, ``usage``)

    Auth is ``Authorization: Bearer $API_SERVER_KEY``. An earlier Dobot adapter invented
    ``/run``, ``/computer`` and ``/browser/submit`` — no such routes exist upstream, which is
    why ``AGENT_RUNTIME=remote`` could never drive the real agent.
    """

    name = "hermes-agent"
    mode = "remote"
    default_endpoint = "http://127.0.0.1:8642"
    terminal_events = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.endpoint = (settings.hermes_endpoint or self.default_endpoint).rstrip("/")
        self.api_key = (settings.hermes_api_key or "").strip()
        self._client: httpx.AsyncClient | None = None
        self._active_run: str = ""
        self.last_health: dict[str, Any] = {}
        #: Last health answer, cached for HERMES_HEALTH_TTL_SECONDS. ``None`` means "not probed
        #: yet", which reads as available so a fresh process still tries the gateway.
        self._reachable: bool | None = None
        self._checked_at: float = 0.0
        self._probe_detail: str = ""

    @property
    def available(self) -> bool:
        """Cached reachability. Call :meth:`probe` to refresh; a call never blocks on a dead host."""
        return True if self._reachable is None else self._reachable

    def _remember(self, reachable: bool, detail: str = "") -> None:
        self._reachable = reachable
        self._checked_at = time.monotonic()
        self._probe_detail = detail

    async def probe(self, *, force: bool = False) -> bool:
        """Refresh reachability when the cached answer is stale. Never raises."""
        ttl = float(getattr(self.settings, "hermes_health_ttl_seconds", 30)
                    or 30)
        if not force and self._checked_at and (time.monotonic() - self._checked_at) < ttl:
            return bool(self._reachable)
        timeout = float(getattr(self.settings, "hermes_probe_timeout_seconds", 2.5) or 2.5)
        result = await self.health(timeout=timeout)
        self._remember(bool(result.get("ok")), str(result.get("error") or ""))
        return bool(result.get("ok"))

    # ------------------------------------------------------------------ transport

    def _headers(self, *, stream: bool = False) -> dict[str, str]:
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.endpoint,
                timeout=httpx.Timeout(float(self.settings.hermes_timeout_seconds), connect=5.0),
            )
        return self._client

    async def health(self, *, timeout: float = 2.5) -> dict[str, Any]:
        """Probe the gateway once. Never raises: returns ``{"ok": bool, ...}``."""
        try:
            response = await self._http().get("/v1/health", headers=self._headers(),
                                             timeout=timeout)
            if response.status_code == 401:
                return {"ok": False, "error": "401 unauthorized — set HERMES_API_KEY to the "
                                               "gateway's API_SERVER_KEY"}
            if response.status_code == 404:
                # Older gateways expose only /health.
                response = await self._http().get("/health", headers=self._headers(),
                                                  timeout=timeout)
            response.raise_for_status()
            payload = response.json() if response.content else {"status": "ok"}
            self.last_health = payload if isinstance(payload, dict) else {"status": "ok"}
            self._remember(True)
            return {"ok": True, **self.last_health}
        except Exception as exc:  # noqa: BLE001
            self._remember(False, str(exc))
            return {"ok": False, "error": f"Hermes gateway unreachable at {self.endpoint}: {exc}"}

    async def info(self) -> RuntimeInfo:
        probe = await self.health()
        version = str(self.last_health.get("version") or self.last_health.get("gateway") or "")
        detail = (
            f"Hermes Agent gateway at {self.endpoint}"
            + (f" ({version})" if version else "")
            + ("" if probe.get("ok") else f" — not reachable: {probe.get('error', '')}")
        )
        return RuntimeInfo(
            mode=self.mode,
            name=self.name,
            available=bool(probe.get("ok")),
            detail=detail,
            toolsets=self.settings.hermes_tools,
        )

    # ------------------------------------------------------------------ runs

    async def _create_run(self, prompt: str, session_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"input": prompt}
        if session_id:
            payload["session_id"] = session_id
        response = await self._http().post("/v1/runs", json=payload, headers=self._headers())
        if response.status_code == 401:
            return {"ok": False, "error": "Hermes rejected the API key (401). Set HERMES_API_KEY "
                                           "to the gateway's API_SERVER_KEY.", "status": 401}
        if response.status_code == 404:
            return {"ok": False, "not_found": True, "status": 404}
        response.raise_for_status()
        data = response.json()
        run_id = str(data.get("run_id") or data.get("id") or "")
        if not run_id:
            return {"ok": False, "error": f"unexpected /v1/runs response: {data}"}
        self._active_run = run_id
        return {"ok": True, "run_id": run_id, **data}

    async def _stream_run(self, run_id: str, deadline: float) -> dict[str, Any]:
        """Consume the run's SSE stream until a terminal event or the deadline."""
        output = ""
        error = ""
        status = ""
        tools: list[dict[str, Any]] = []
        deltas: list[str] = []
        async with self._http().stream(
            "GET", f"/v1/runs/{run_id}/events", headers=self._headers(stream=True),
            timeout=httpx.Timeout(deadline),
        ) as response:
            if response.status_code >= 400:
                return {"ok": False, "error": f"event stream HTTP {response.status_code}",
                        "status_code": response.status_code}
            sse_event = ""
            async for line in response.aiter_lines():
                if not line:
                    sse_event = ""
                    continue
                if line.startswith("event:"):
                    sse_event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    if raw == "[DONE]":
                        break
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("event") or event.get("type") or sse_event or "")
                if kind in ("tool.started", "tool.completed"):
                    tools.append({"event": kind, "tool": event.get("tool", ""),
                                  "preview": _trim(event.get("preview"), 300)})
                elif kind in ("delta", "token", "message.delta"):
                    text = event.get("delta") or event.get("text") or ""
                    if isinstance(text, str):
                        deltas.append(text)
                if kind in self.terminal_events:
                    status = kind.split(".", 1)[-1]
                    output = str(event.get("output") or output or "".join(deltas))
                    error = str(event.get("error") or "")
                    break
        if status not in {"completed", "failed", "cancelled", "interrupted"}:
            polled = await self._poll_run(run_id)
            status = polled.get("status") or status or "unknown"
            output = output or polled.get("output", "")
            error = error or polled.get("error", "")
        return {
            "ok": status == "completed",
            "status": status,
            "output": output,
            "stdout": output,
            "error": (error or None) if status != "completed" else None,
            "run_id": run_id,
            "tools": tools,
        }

    async def _poll_run(self, run_id: str) -> dict[str, Any]:
        try:
            response = await self._http().get(f"/v1/runs/{run_id}", headers=self._headers())
            if response.status_code >= 400:
                return {}
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _openai_fallback(self, prompt: str) -> dict[str, Any]:
        """Plain Chat Completions, for gateways/older builds without the runs API."""
        try:
            response = await self._http().post(
                "/v1/chat/completions",
                json={"model": "hermes-agent",
                      "messages": [{"role": "user", "content": prompt}]},
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
            text = ""
            choices = data.get("choices") or []
            if choices:
                text = str((choices[0].get("message") or {}).get("content") or "")
            return {"ok": True, "status": "completed", "output": text, "stdout": text,
                    "tools": [], "fallback": "chat.completions"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Hermes call failed: {exc}"}

    async def execute(
        self,
        prompt: str,
        *,
        session_id: str = "",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run one agent turn on the real gateway and return the tool trace."""
        budget = float(timeout or self.settings.hermes_timeout_seconds)
        created = await self._create_run(prompt, session_id=session_id or self.settings.hermes_session_id)
        if not created.get("ok"):
            if created.get("not_found"):
                return await self._openai_fallback(prompt)
            return created
        try:
            result = await asyncio.wait_for(
                self._stream_run(created["run_id"], budget), timeout=budget
            )
        except TimeoutError:
            await self._stop(created["run_id"])
            return {"ok": False, "status": "timeout", "run_id": created["run_id"],
                    "error": f"Hermes run exceeded {budget:.0f}s and was stopped"}
        finally:
            self._active_run = ""
        return result

    async def _stop(self, run_id: str) -> None:
        try:
            await self._http().post(f"/v1/runs/{run_id}/stop", json={}, headers=self._headers())
        except Exception:  # noqa: BLE001
            logger.debug("could not stop Hermes run %s", run_id, exc_info=True)

    # ------------------------------------------------------------------ runtime interface

    async def computer_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "Perform exactly this desktop action with the computer_use toolset, then report "
            f"what the screen shows. action={action} params={json.dumps(params, ensure_ascii=False)}. "
            "Do not take any other action."
        )
        result = await self.execute(prompt)
        if result.get("ok"):
            result["evidence"] = {"runtime_output": _trim(result.get("output"), 800),
                                  "tools": result.get("tools", [])}
            result["expected_window"] = params.get("app") or params.get("target")
        return result

    async def browser_submit(
        self, *, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]:
        prompt = (
            f"Open {url} in the browser, fill the form with "
            f"{json.dumps(fields, ensure_ascii=False)}, submit with selector "
            f"'{submit_selector or 'auto'}', then report the resulting page title and "
            "confirmation text. Do not submit anything else."
        )
        result = await self.execute(prompt)
        if result.get("ok"):
            result["confirmation"] = _trim(result.get("output"), 400)
            result["page_changed"] = True
        return result

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]:
        if toolsets and toolsets != self.settings.hermes_tools:
            # Toolsets are gateway config upstream; naming them keeps the intent in the trace.
            prompt = f"[toolsets: {', '.join(toolsets)}]\n{prompt}"
        return await self.execute(prompt)

    async def close(self) -> None:
        if self._active_run:
            await self._stop(self._active_run)
        if self._client is not None:
            await self._client.aclose()


class ResilientRuntime:
    """Hermes when the gateway answers, Dobot's own guarded tools when it does not.

    A hosted deployment (one container, one exposed port) cannot assume the Hermes gateway is up:
    it may still be booting, it may have been disabled, or the process may have died. Dobot keeps
    answering chat, files, terminal, browser and research in that state — only the capabilities
    that genuinely live in Hermes (desktop gestures, whole-agent delegation) report unavailable,
    and they say why instead of timing out.

    ``available`` is the cached health answer, so planning and tool listing never block on a dead
    host; ``probe()`` refreshes it on the configured TTL.
    """

    name = "hermes-agent+local"
    mode = "auto"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.primary = HermesApiRuntime(settings)
        self.fallback = LocalRuntime(settings)
        self._probe_detail = ""

    @property
    def available(self) -> bool:
        return self.primary.available

    async def probe(self, *, force: bool = False) -> bool:
        ok = await self.primary.probe(force=force)
        self._probe_detail = self.primary._probe_detail
        return ok

    def _down(self, capability: str) -> dict[str, Any]:
        return {
            "ok": False,
            "degraded": True,
            "fallback": "local",
            "error": (
                f"{capability} needs the Hermes agent, and its gateway at {self.primary.endpoint} "
                "is not reachable. Dobot's own tools keep working; start the gateway "
                "(`hermes gateway run`) or set HERMES_ENDPOINT to a live one."
                + (f" Last probe: {self._probe_detail}" if self._probe_detail else "")
            ),
        }

    async def info(self) -> RuntimeInfo:
        probe = await self.primary.health()
        if probe.get("ok"):
            base = await self.primary.info()
            return RuntimeInfo(
                mode="auto",
                name="hermes-agent",
                available=True,
                detail=base.detail,
                toolsets=self.settings.hermes_tools,
            )
        return RuntimeInfo(
            mode="auto",
            name="dobot-local-tools",
            available=True,  # Dobot is up; only the Hermes-backed capabilities are not
            detail=(
                f"Hermes gateway not reachable at {self.primary.endpoint} — running on Dobot's "
                "own tools (no desktop control, no agent delegation)"
            ),
            toolsets=["filesystem", "terminal", "screen", "web"],
        )

    async def computer_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if not await self.primary.probe():
            return self._down(f"desktop action '{action}'")
        result = await self.primary.computer_action(action, params)
        return result

    async def browser_submit(
        self, *, url: str, fields: dict[str, Any], submit_selector: str = ""
    ) -> dict[str, Any]:
        if not await self.primary.probe():
            return self._down("form submission through a live browser session")
        return await self.primary.browser_submit(
            url=url, fields=fields, submit_selector=submit_selector
        )

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]:
        if not await self.primary.probe():
            return self._down("delegating a task to the agent")
        return await self.primary.run_prompt(prompt, toolsets=toolsets)

    async def close(self) -> None:
        await self.primary.close()


def _trim(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_runtime(settings: Settings | None = None) -> ExecutionRuntime:
    settings = settings or get_settings()
    mode = settings.agent_runtime
    fallback = bool(getattr(settings, "hermes_fallback_local", True))
    if mode in {"remote", "api", "hermes"}:
        runtime = HermesApiRuntime(settings)
        return ResilientRuntime(settings) if fallback else runtime
    if mode == "cli":
        runtime = HermesCliRuntime(settings)
        if runtime.available:
            return runtime
        logger.warning("AGENT_RUNTIME=cli but the Hermes CLI was not found; using local tools")
        return LocalRuntime(settings)
    if mode == "auto":
        if fallback:
            return ResilientRuntime(settings)
        api = HermesApiRuntime(settings)
        if api.endpoint:
            return api
        cli = HermesCliRuntime(settings)
        if cli.available:
            return cli
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
