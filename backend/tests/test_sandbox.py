"""Sandbox backends: provider selection, honest status, and the Nebius (ConTree) executor.

Nothing here touches the network: ``NebiusSandbox`` is exercised through its seams (``_execute``,
``_probe_sync``), because the real ConTree API needs live credentials. The live path is covered by
``status()`` degrading to a truthful ``isolation: none`` instead of pretending.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.sandbox import (
    FallbackSandbox,
    LocalSandbox,
    NebiusSandbox,
    SandboxUnavailable,
    build_sandbox,
)


def _nebius_settings(settings, *, key: str = "test-key", project: str = "test-project"):
    return settings.model_copy(
        update={
            "sandbox_provider": "nebius",
            "nebius_api_key": key,
            "nebius_project_id": project,
        }
    )


# --------------------------------------------------------------- provider selection


def test_default_provider_is_the_local_firewall(settings) -> None:
    sandbox = build_sandbox(settings)
    assert isinstance(sandbox, FallbackSandbox)
    assert sandbox.name == "action-firewall"
    # Requesting "local" means no remote sandbox was asked for: degraded from birth.
    assert sandbox.degraded


def test_nebius_provider_builds_the_nebius_backend(settings) -> None:
    sandbox = build_sandbox(_nebius_settings(settings))
    assert isinstance(sandbox._primary, NebiusSandbox)
    assert not sandbox.degraded
    assert sandbox.name == "nebius"


def test_unknown_provider_falls_back_to_local(settings) -> None:
    copied = settings.model_copy(update={"sandbox_provider": "banana"})
    # The validator is what normalises unknown values; model_copy skips it, so ask Settings itself.
    from app.config import Settings

    resolved = Settings(sandbox_provider="banana")
    assert resolved.sandbox_provider == "local"
    assert copied.sandbox_provider == "banana"  # copied verbatim — validation happened at load


def test_nebius_requires_key_and_project(settings) -> None:
    no_key = NebiusSandbox(_nebius_settings(settings, key=""), LocalSandbox(settings))
    with pytest.raises(SandboxUnavailable, match="NEBIUS_API_KEY"):
        no_key._connect()
    no_project = NebiusSandbox(_nebius_settings(settings, project=""), LocalSandbox(settings))
    with pytest.raises(SandboxUnavailable, match="NEBIUS_PROJECT_ID"):
        no_project._connect()


# ------------------------------------------------------------------- status honesty


@pytest.mark.asyncio
async def test_nebius_status_without_credentials_is_none_isolation(settings) -> None:
    sandbox = NebiusSandbox(
        _nebius_settings(settings, key=""),
        LocalSandbox(settings),
    )
    status = await sandbox.status()
    assert status.provider == "nebius"
    assert status.isolation == "none"
    assert status.available is False
    assert status.degraded is True
    assert "NEBIUS_API_KEY" in str(status.details.get("reason", ""))


@pytest.mark.asyncio
async def test_nebius_status_reports_vm_when_reachable(settings, monkeypatch) -> None:
    sandbox = NebiusSandbox(_nebius_settings(settings), LocalSandbox(settings))
    monkeypatch.setattr(
        sandbox, "_probe_sync", lambda: {"reachable": True, "images": 3}, raising=True
    )
    status = await sandbox.status()
    assert status.isolation == "vm"
    assert status.available is True
    assert status.degraded is False
    assert status.details["image"] == settings.nebius_sandbox_image
    # Second call inside the TTL must not probe again (page loads never hammer the API).
    monkeypatch.setattr(
        sandbox, "_probe_sync", lambda: (_ for _ in ()).throw(AssertionError("probed twice")),
        raising=True,
    )
    cached = await sandbox.status()
    assert cached.isolation == "vm"


@pytest.mark.asyncio
async def test_fallback_marks_unreachable_primary_degraded(settings) -> None:
    nebius = NebiusSandbox(_nebius_settings(settings, key=""), LocalSandbox(settings))
    fallback = FallbackSandbox(nebius, LocalSandbox(settings), _nebius_settings(settings))
    status = await fallback.status()
    assert status.degraded is True
    assert status.isolation == "none"


# ------------------------------------------------------------------ command routing


class _FakeRun:
    """Stands in for a finished ConTree image state."""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.asyncio
async def test_nebius_run_command_maps_result_and_keeps_session_state(
    settings, monkeypatch
) -> None:
    sandbox = NebiusSandbox(_nebius_settings(settings), LocalSandbox(settings))
    seen: list[object] = []
    runs: list[_FakeRun] = []

    def fake_execute(command: str, cwd, timeout, env):
        seen.append(sandbox._state)
        run = _FakeRun(stdout=f"ran:{command}")
        runs.append(run)
        return run

    monkeypatch.setattr(sandbox, "_execute", fake_execute, raising=True)
    first = await sandbox.run_command("echo one")
    assert first.ok and first.backend == "nebius"
    assert first.stdout == "ran:echo one"
    second = await sandbox.run_command("echo two")
    assert second.ok
    # The second command chained off the first one's state — the session persists.
    assert seen[0] is None
    assert seen[1] is runs[0]


@pytest.mark.asyncio
async def test_nebius_run_command_nonzero_exit_is_a_result_not_an_error(settings, monkeypatch) -> None:
    sandbox = NebiusSandbox(_nebius_settings(settings), LocalSandbox(settings))
    monkeypatch.setattr(
        sandbox,
        "_execute",
        lambda *args: _FakeRun(exit_code=2, stderr="boom"),
        raising=True,
    )
    result = await sandbox.run_command("false")
    assert not result.ok
    assert result.exit_code == 2
    assert result.stderr == "boom"


@pytest.mark.asyncio
async def test_nebius_transport_failure_becomes_unavailable(settings, monkeypatch) -> None:
    sandbox = NebiusSandbox(_nebius_settings(settings), LocalSandbox(settings))

    def explode(*args):
        raise ConnectionError("dns down")

    monkeypatch.setattr(sandbox, "_execute", explode, raising=True)
    with pytest.raises(SandboxUnavailable, match="unreachable"):
        await sandbox.run_command("echo hi")


@pytest.mark.asyncio
async def test_fallback_degrades_to_the_firewall_when_nebius_is_unavailable(
    settings, monkeypatch
) -> None:
    conf = _nebius_settings(settings, key="")  # credentials missing -> SandboxUnavailable
    sandbox = build_sandbox(conf)
    result = await sandbox.run_command("echo hi")
    # The command still ran — locally, through the action firewall — and the wrapper says which.
    assert result.ok
    assert result.backend == "action-firewall"
    assert sandbox.degraded is True
    status = await sandbox.status()
    assert status.provider == "local"
    assert status.isolation == "none"
    assert status.degraded is True


def test_shell_wrapping_creates_the_remote_cwd(settings) -> None:
    shell = NebiusSandbox._shell_for("run tests", "/tmp/work")
    assert "mkdir -p /tmp/work" in shell
    assert "cd /tmp/work" in shell
    assert "run tests" in shell
    assert NebiusSandbox._shell_for("run tests", None) == "run tests"


@pytest.mark.asyncio
async def test_fallback_close_tolerates_a_non_httpx_client(settings) -> None:
    nebius = NebiusSandbox(_nebius_settings(settings), LocalSandbox(settings))
    nebius._client = SimpleNamespace()  # contree clients have no aclose()
    fallback = FallbackSandbox(nebius, LocalSandbox(settings), _nebius_settings(settings))
    await fallback.close()  # must not raise
