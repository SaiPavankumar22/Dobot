"""Tests for the Hermes Agent gateway adapter.

The gateway is faked with ``httpx.MockTransport``: no socket, no browser, no network. The
contract under test is the upstream one — ``POST /v1/runs`` then ``GET /v1/runs/{id}/events``
(SSE) — because the previous adapter called routes that do not exist upstream.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agents.hermes import HermesApiRuntime, LocalRuntime, ResilientRuntime, build_runtime
from app.agents.sandbox import build_sandbox
from app.config import Settings
from app.tools.base import ToolContext
from app.tools.hermes_task import HermesTaskTool
from app.tools.registry import default_registry


def _runtime(settings: Settings, handler: Callable[[httpx.Request], httpx.Response]) -> HermesApiRuntime:
    runtime = HermesApiRuntime(settings.model_copy(update={"hermes_endpoint": "http://hermes.test"}))
    runtime._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://hermes.test"
    )
    return runtime


def _sse(*events: dict[str, Any]) -> bytes:
    body = ""
    for event in events:
        body += f"data: {json.dumps(event)}\n\n"
    return body.encode()


def _gateway(events: list[dict[str, Any]], *, captured: list[httpx.Request] | None = None,
             runs_status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"status": "ok", "version": "0.9.1"})
        if request.url.path == "/v1/runs" and request.method == "POST":
            if runs_status != 200:
                return httpx.Response(runs_status, json={"error": "nope"})
            return httpx.Response(200, json={"run_id": "run_abc", "status": "queued"})
        if request.url.path == "/v1/runs/run_abc/events":
            return httpx.Response(200, content=_sse(*events),
                                  headers={"content-type": "text/event-stream"})
        if request.url.path == "/v1/runs/run_abc":
            return httpx.Response(200, json={"status": "completed", "output": "polled"})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "fallback"}}]})
        return httpx.Response(404, json={"error": f"no route {request.url.path}"})

    return handler


# ------------------------------------------------------------------ happy path


async def test_run_streams_tool_trace_and_final_output(settings: Settings) -> None:
    events = [
        {"event": "run.started", "run_id": "run_abc"},
        {"event": "tool.started", "tool": "computer_use", "preview": "click ok"},
        {"event": "tool.completed", "tool": "computer_use", "duration": 0.4, "error": False},
        {"event": "run.completed", "output": "Clicked OK. Window shows the save dialog."},
    ]
    captured: list[httpx.Request] = []
    runtime = _runtime(settings, _gateway(events, captured=captured))

    result = await runtime.run_prompt("click ok")

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["output"] == "Clicked OK. Window shows the save dialog."
    assert [t["tool"] for t in result["tools"]] == ["computer_use", "computer_use"]
    posted = next(r for r in captured if r.url.path == "/v1/runs")
    assert json.loads(posted.content) == {"input": "click ok"}
    await runtime.close()


async def test_request_carries_bearer_key(settings: Settings) -> None:
    captured: list[httpx.Request] = []
    runtime = _runtime(
        settings.model_copy(update={"hermes_api_key": "secret-key"}),
        _gateway([{"event": "run.completed", "output": "done"}], captured=captured),
    )
    await runtime.run_prompt("hi")
    assert captured[0].headers["authorization"] == "Bearer secret-key"
    await runtime.close()


async def test_tool_events_are_exposed_for_computer_action(settings: Settings) -> None:
    events = [
        {"event": "tool.started", "tool": "computer_use"},
        {"event": "tool.completed", "tool": "computer_use"},
        {"event": "run.completed", "output": "Notepad is open."},
    ]
    runtime = _runtime(settings, _gateway(events))
    result = await runtime.computer_action("open", {"app": "notepad"})
    assert result["ok"] is True
    assert result["expected_window"] == "notepad"
    assert result["evidence"]["tools"][0]["tool"] == "computer_use"
    await runtime.close()


# ------------------------------------------------------------------ failure honesty


async def test_missing_runs_route_falls_back_to_chat_completions(settings: Settings) -> None:
    runtime = _runtime(settings, _gateway([], runs_status=404))
    result = await runtime.run_prompt("hello")
    assert result["ok"] is True
    assert result["fallback"] == "chat.completions"
    assert result["output"] == "fallback"
    await runtime.close()


async def test_unauthorized_key_is_reported_not_swallowed(settings: Settings) -> None:
    runtime = _runtime(settings, _gateway([], runs_status=401))
    result = await runtime.run_prompt("hello")
    assert result["ok"] is False
    assert "401" in result["error"]
    await runtime.close()


async def test_failed_run_is_not_ok(settings: Settings) -> None:
    runtime = _runtime(settings, _gateway([{"event": "run.failed", "error": "model refused"}]))
    result = await runtime.run_prompt("do it")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "refused" in result["error"]
    await runtime.close()


async def test_stream_without_terminal_event_polls_run_status(settings: Settings) -> None:
    runtime = _runtime(settings, _gateway([{"event": "tool.started", "tool": "terminal"}]))
    result = await runtime.run_prompt("run ls")
    assert result["ok"] is True
    assert result["output"] == "polled"
    await runtime.close()


# ------------------------------------------------------------------ discovery / wiring


async def test_health_probe_reports_version(settings: Settings) -> None:
    runtime = _runtime(settings, _gateway([]))
    probe = await runtime.health()
    assert probe["ok"] is True
    info = await runtime.info()
    assert info.available is True
    assert "0.9.1" in info.detail
    await runtime.close()


def test_build_runtime_selects_the_gateway_for_remote_aliases(settings: Settings) -> None:
    """Remote modes reach the gateway, wrapped so a dead gateway degrades instead of hard-failing."""
    for mode in ("remote", "api", "hermes", "auto"):
        runtime = build_runtime(settings.model_copy(update={"agent_runtime": mode}))
        assert isinstance(runtime, ResilientRuntime), mode
        assert isinstance(runtime.primary, HermesApiRuntime), mode
        assert isinstance(runtime.fallback, LocalRuntime), mode


def test_build_runtime_strict_mode_drops_the_fallback(settings: Settings) -> None:
    """hermes_fallback_local=false is the explicit 'Hermes or nothing' contract."""
    for mode in ("remote", "api", "hermes", "auto"):
        strict = settings.model_copy(
            update={"agent_runtime": mode, "hermes_fallback_local": False}
        )
        assert isinstance(build_runtime(strict), HermesApiRuntime), mode


def test_build_runtime_defaults_to_local_tools(settings: Settings) -> None:
    assert isinstance(build_runtime(settings), LocalRuntime)


def test_gateway_endpoint_defaults_to_loopback(settings: Settings) -> None:
    runtime = HermesApiRuntime(settings.model_copy(update={"hermes_endpoint": ""}))
    assert runtime.endpoint == "http://127.0.0.1:8642"


@pytest.mark.parametrize("mode", ["nonsense", "REMOTE"])
def test_runtime_mode_validation(settings: Settings, mode: str) -> None:
    validated = Settings.model_validate(settings.model_dump() | {"agent_runtime": mode})
    assert validated.agent_runtime == ("remote" if mode == "REMOTE" else "local")


# ------------------------------------------------------------------ hermes_run delegation


class _StubRuntime:
    name = "hermes-agent"
    available = True

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result or {"ok": True, "output": "chart added", "run_id": "run_1",
                                 "tools": [{"event": "tool.started", "tool": "computer_use"}]}
        self.calls: list[tuple[str, Any]] = []

    async def run_prompt(self, prompt: str, *, toolsets: list[str] | None = None) -> dict[str, Any]:
        self.calls.append((prompt, toolsets))
        return self.result


def _ctx(settings: Settings, workspace: Path, runtime: Any) -> ToolContext:
    return ToolContext(
        settings=settings, sandbox=build_sandbox(settings), bus=None, task_id="t", hermes=runtime
    )


def test_registry_exposes_hermes_run() -> None:
    assert default_registry().get("hermes_run") is not None


async def test_hermes_run_delegates_goal_and_records_trace(
    settings: Settings, workspace: Path
) -> None:
    runtime = _StubRuntime()
    tool = HermesTaskTool()
    result = await tool.run(
        {"goal": "chart revenue in Excel", "context": "report.xlsx",
         "toolsets": ["computer_use"]},
        _ctx(settings, workspace, runtime),
    )
    assert result.ok is True
    prompt, toolsets = runtime.calls[0]
    assert "chart revenue in Excel" in prompt and "report.xlsx" in prompt
    assert toolsets == ["computer_use"]
    assert result.output["evidence"]["tool_trace"][0]["tool"] == "computer_use"


async def test_hermes_run_is_honest_without_a_runtime(settings: Settings, workspace: Path) -> None:
    result = await HermesTaskTool().run(
        {"goal": "anything"}, _ctx(settings, workspace, LocalRuntime(settings))
    )
    assert result.ok is False
    assert "Hermes" in (result.error or "")


async def test_hermes_run_reports_runtime_failure(settings: Settings, workspace: Path) -> None:
    runtime = _StubRuntime({"ok": False, "error": "gateway refused: out of credits"})
    result = await HermesTaskTool().run({"goal": "x"}, _ctx(settings, workspace, runtime))
    assert result.ok is False
    assert "credits" in (result.error or "")
