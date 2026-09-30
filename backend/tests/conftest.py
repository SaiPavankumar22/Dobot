"""Test fixtures: an isolated state directory and a fully wired service graph with no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Settings, reset_settings_cache
from app.events import EventBus, set_event_bus


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Offline settings: no providers, state confined to a temp dir, workspace inside tmp."""
    monkeypatch.setenv("DOBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DOBOT_ENV", "test")
    monkeypatch.setenv("NEBIUS_API_KEY", "")
    # The developer's .env carries a real project id; never let it reach the sandbox backend in tests.
    monkeypatch.setenv("NEBIUS_PROJECT_ID", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("MONGODB_URI", "")
    monkeypatch.setenv("ZILLIZ_URI", "")
    monkeypatch.setenv("SANDBOX_PROVIDER", "local")
    monkeypatch.setenv("AGENT_RUNTIME", "local")
    monkeypatch.setenv("SHADOW_MODE", "false")
    # The developer's own .env may opt into stricter confirmation. Tests assert the specification's
    # defaults, so pin it here; the tests that exercise the strict mode turn it on explicitly.
    monkeypatch.setenv("REQUIRE_WRITE_APPROVAL", "false")
    monkeypatch.setenv("SANDBOX_ALLOWED_PATHS", str(tmp_path))
    monkeypatch.setenv("SANDBOX_ALLOWED_NETWORK", "example.com,localhost")
    monkeypatch.setenv("DOBOT_SKILLS_DIR", str(tmp_path / "skills"))
    # V3 subsystems, pinned for the same reason: a developer's own .env must not change what the
    # suite asserts. Tests that exercise a non-default value set it explicitly.
    monkeypatch.setenv("DOBOT_API_TOKEN", "")
    monkeypatch.setenv("VOICE_ENABLED", "false")
    monkeypatch.setenv("CONSOLIDATION_ENABLED", "false")
    monkeypatch.setenv("INTERCEPTORS_ENABLED", "true")
    monkeypatch.setenv("TOKENJUICE_ENABLED", "true")
    monkeypatch.setenv("MEMORY_DECAY_ENABLED", "true")
    monkeypatch.setenv("PRICE_PER_MTOK_INPUT", "0")
    monkeypatch.setenv("PRICE_PER_MTOK_OUTPUT", "0")
    monkeypatch.setenv("PRINCIPAL_NAME", "")
    # Point "home" at the temp dir so "~/..." plans stay inside the sandbox and never touch the
    # developer's real filesystem during tests.
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    reset_settings_cache()
    settings = Settings()
    # Settings.state_dir is derived from the repo root when relative; point it at tmp explicitly.
    object.__setattr__(settings, "dobot_state_dir", str(tmp_path / "state"))
    return settings


@pytest.fixture()
def bus() -> EventBus:
    test_bus = EventBus()
    set_event_bus(test_bus)
    return test_bus


@pytest.fixture()
async def services(settings: Settings, bus: EventBus):
    from app.services import build_services

    graph = await build_services(settings, bus=bus)
    await graph.startup()
    try:
        yield graph
    finally:
        await graph.shutdown()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def _clean_env() -> None:
    """Keep the developer's real .env, and any earlier test's environment, out of each test."""
    for key in ("HERMES_ENDPOINT", "OPENSHIELD_GATEWAY_URL"):
        os.environ.pop(key, None)
    yield
    # `get_settings()` is lru_cached. Without this, a test that ran with a redirected state directory
    # leaves that cached Settings behind and the *next* test silently inherits it, which is how a suite
    # starts passing for the wrong reason.
    reset_settings_cache()
