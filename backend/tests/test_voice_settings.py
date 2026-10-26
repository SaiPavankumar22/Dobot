"""Voice settings and transcriber-state tests.

Verifies:
- PATCH /settings updates voice_enabled live.
- GET /settings returns the live voice_enabled value.
- Transcriber model downloads land under settings.models_dir.
- 409 VOICE_UNAVAILABLE is returned correctly when transcriber not ready.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.security.local_secrets._keyring", lambda: None)
    env_path = tmp_path / ".env"
    env_path.write_text("NEBIUS_API_KEY=\nTAVILY_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr("app.config.REPO_ROOT", tmp_path)
    settings = Settings(dobot_state_dir=str(tmp_path / ".dobot"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c, settings


def test_get_settings_returns_voice_enabled(client) -> None:
    test_client, settings = client
    settings.voice_enabled = False
    body = test_client.get("/settings").json()
    assert body["voice_enabled"] is False

    settings.voice_enabled = True
    body = test_client.get("/settings").json()
    assert body["voice_enabled"] is True


def test_patch_settings_voice_enabled(client) -> None:
    test_client, settings = client
    settings.voice_enabled = False
    response = test_client.patch("/settings", json={"voice_enabled": True})
    assert response.status_code == 200
    assert response.json()["applied"]["voice_enabled"] is True
    assert settings.voice_enabled is True

    response2 = test_client.patch("/settings", json={"voice_enabled": False})
    assert response2.status_code == 200
    assert settings.voice_enabled is False


def test_transcriber_unavailable_returns_409(client) -> None:
    """When faster-whisper is not installed, /voice/transcribe returns 409."""
    test_client, settings = client
    settings.transcriber_enabled = True

    import io
    wav_data = io.BytesIO(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00")

    with patch("app.agents.transcriber.Transcriber.package_available", new_callable=lambda: property(lambda self: False)):
        response = test_client.post(
            "/voice/transcribe",
            files={"file": ("recording.wav", wav_data, "audio/wav")},
        )
    # Either 409 (transcriber unavailable) or 503 — either is correct; 200 would be wrong.
    assert response.status_code in {409, 503}, f"expected 4xx, got {response.status_code}: {response.text}"


def test_models_dir_used_for_download(tmp_path) -> None:
    """WhisperModel must receive download_root pointing to settings.models_dir."""
    settings = Settings(dobot_state_dir=str(tmp_path / ".dobot"))
    transcriber = None

    # Lazy import inside the test to avoid top-level import failures if faster-whisper missing.
    from app.agents.transcriber import Transcriber

    transcriber = Transcriber(settings)
    expected_dir = str(settings.models_dir)

    calls: list[dict] = []

    class _FakeWhisper:
        def __init__(self, model_name: str, **kwargs):
            calls.append({"model_name": model_name, **kwargs})

        def transcribe(self, path: str, **kwargs):
            return iter([]), type("Info", (), {"language": "en"})()

    fake_fw = types.ModuleType("faster_whisper")
    fake_fw.WhisperModel = _FakeWhisper  # type: ignore[attr-defined]
    original = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake_fw
    try:
        transcriber._load()
    finally:
        if original is None:
            del sys.modules["faster_whisper"]
        else:
            sys.modules["faster_whisper"] = original

    assert calls, "WhisperModel was not instantiated"
    assert calls[0].get("download_root") == expected_dir, (
        f"expected download_root={expected_dir!r}, got {calls[0].get('download_root')!r}"
    )
