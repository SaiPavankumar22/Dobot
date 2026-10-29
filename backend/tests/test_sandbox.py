"""Tests for the local action-firewall sandbox."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.sandbox import FallbackSandbox, LocalSandbox, SandboxDenied, build_sandbox


def test_default_provider_is_the_local_firewall(settings) -> None:
    sandbox = build_sandbox(settings)
    assert isinstance(sandbox, FallbackSandbox)
    assert sandbox.name == "action-firewall"
    assert sandbox.degraded


def test_unknown_provider_is_normalized_to_local(settings) -> None:
    from app.config import Settings

    resolved = Settings(sandbox_provider="nebius")
    assert resolved.sandbox_provider == "local"
    assert settings.sandbox_provider == "local"


@pytest.mark.asyncio
async def test_local_run_command_expands_home_cwd(settings) -> None:
    sandbox = LocalSandbox(settings)
    result = await sandbox.run_command("cd" if os.name == "nt" else "pwd", cwd="~")
    assert result.ok, result.stderr
    assert "~" not in result.stdout.strip()
    assert Path(result.stdout.strip()).resolve() == Path.home().resolve()


@pytest.mark.asyncio
async def test_local_run_command_rejects_cwd_outside_the_roots(settings) -> None:
    sandbox = LocalSandbox(settings)
    with pytest.raises(SandboxDenied):
        await sandbox.run_command("echo nope", cwd="C:/Windows/Temp")
