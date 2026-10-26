"""Credential management from Settings: secure local persistence, masking, and hot reload."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Force the encrypted fallback so CI does not depend on an OS credential manager.
    monkeypatch.setattr("app.security.local_secrets._keyring", lambda: None)
    env_path = tmp_path / ".env"
    env_path.write_text("NEBIUS_API_KEY=\nTAVILY_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr("app.config.REPO_ROOT", tmp_path)
    settings = Settings(dobot_state_dir=str(tmp_path / ".dobot"))
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, env_path


def test_keys_are_listed_masked(client) -> None:
    test_client, _env = client
    body = test_client.get("/settings/keys").json()
    ids = {key["id"] for key in body["keys"]}
    # Exactly the keys a person brings for themselves — nothing infrastructure-shaped. Semantic
    # memory runs on the local index, so there is no hosted vector database to configure.
    assert ids == {"nebius", "tavily", "novita"}
    for key in body["keys"]:
        assert "value" not in key  # raw secrets never cross the boundary
        assert isinstance(key["set"], bool)


def test_operator_managed_values_are_reported_but_not_writable(client) -> None:
    test_client, _env = client
    reasons = {item["id"]: item["why"] for item in test_client.get("/settings/keys").json()["operator"]}
    assert "mongodb_uri" in reasons and "langsmith_api_key" in reasons
    # The judgement key belongs to the person, not the operator — Laya's old operator entries are gone.
    assert "laya_api_key" not in reasons and "laya_server_url" not in reasons
    assert all("value" not in item for item in test_client.get("/settings/keys").json()["operator"])
    for name in ("mongodb_uri", "langsmith", "dobot_api_token"):
        response = test_client.put(f"/settings/keys/{name}", json={"value": "x"})
        assert response.status_code == 403, name
        assert response.json()["error"]["code"] == "OPERATOR_MANAGED"
    assert test_client.delete("/settings/keys/mongodb_uri").status_code == 403


def test_setting_a_key_persists_and_applies(client) -> None:
    test_client, env_path = client
    response = test_client.put("/settings/keys/tavily", json={"value": "tvly-test-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["field"] == "tavily_api_key"
    secret_file = env_path.parent / ".dobot" / "secrets.enc"
    assert secret_file.exists()
    assert b"tvly-test-123" not in secret_file.read_bytes()
    # masked listing now reports it as set without ever exposing the value
    listing = test_client.get("/settings/keys").json()["keys"]
    tavily = next(key for key in listing if key["id"] == "tavily")
    assert tavily["set"] is True
    assert "tvly-test-123" not in tavily["masked"]


def test_setting_a_key_updates_live_settings(client) -> None:
    test_client, _env_path = client
    test_client.put("/settings/keys/nebius", json={"value": "nkey-live"})
    assert test_client.app.state.services.settings.nebius_api_key == "nkey-live"


def test_novita_key_persists_and_reaches_the_judgement_layer(client) -> None:
    """The judgement key is a first-class user key: persisted, masked, and applied to the live
    JEV client so the next judgement re-reads credentials without a restart."""
    test_client, _env_path = client
    response = test_client.put("/settings/keys/novita", json={"value": "sk-novita-123"})
    assert response.status_code == 200
    assert response.json()["field"] == "novita_api_key"
    listing = test_client.get("/settings/keys").json()["keys"]
    novita = next(key for key in listing if key["id"] == "novita")
    assert novita["set"] is True and "sk-novita-123" not in novita["masked"]
    # The live process picked it up: the JEV engine's client sees the key immediately.
    services = test_client.app.state.services
    assert services.settings.novita_api_key == "sk-novita-123"
    assert services.decision.jev.client.available is True


def test_setting_an_existing_key_overwrites_the_line(client) -> None:
    test_client, _env_path = client
    test_client.put("/settings/keys/tavily", json={"value": "first"})
    test_client.put("/settings/keys/tavily", json={"value": "second"})
    assert test_client.app.state.services.settings.tavily_api_key == "second"


def test_clearing_a_key_removes_the_local_secret(client) -> None:
    test_client, _env_path = client
    test_client.put("/settings/keys/tavily", json={"value": "gone-soon"})
    response = test_client.delete("/settings/keys/tavily")
    assert response.status_code == 200
    assert not test_client.app.state.services.settings.tavily_api_key


def test_unknown_key_is_rejected(client) -> None:
    test_client, _env = client
    response = test_client.put("/settings/keys/not_a_key", json={"value": "x"})
    assert response.status_code == 404


def test_empty_value_is_rejected(client) -> None:
    test_client, _env = client
    response = test_client.put("/settings/keys/tavily", json={"value": "   "})
    assert response.status_code == 422


def test_uri_fields_can_be_set(client) -> None:
    test_client, _env_path = client
    # No URI-shaped keys remain, so anything of that shape is unknown — and an unknown key must be
    # refused rather than silently accepted.
    response = test_client.put("/settings/keys/zilliz_uri", json={"value": "https://example.invalid"})
    assert response.status_code == 404
