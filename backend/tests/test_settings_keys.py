"""Credential management from the Settings page: masked listing, persistence to .env, hot reload,
clearing — including behaviour that matters for a packaged install where no editor is assumed."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # A dedicated .env in a temp dir keeps tests away from the user's real credentials.
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
    # Exactly the keys a person brings for themselves — nothing infrastructure-shaped.
    assert ids == {"nebius", "tavily", "zilliz_token", "zilliz_uri"}
    for key in body["keys"]:
        assert "value" not in key  # raw secrets never cross the boundary
        assert isinstance(key["set"], bool)


def test_operator_managed_values_are_reported_but_not_writable(client) -> None:
    test_client, _env = client
    reasons = {item["id"]: item["why"] for item in test_client.get("/settings/keys").json()["operator"]}
    assert "mongodb_uri" in reasons and "langsmith_api_key" in reasons
    assert all("value" not in item for item in test_client.get("/settings/keys").json()["operator"])
    for name in ("mongodb_uri", "langsmith", "dobot_api_token", "laya"):
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
    text = Path(env_path).read_text(encoding="utf-8")
    assert "TAVILY_API_KEY=tvly-test-123" in text
    # masked listing now reports it as set without ever exposing the value
    listing = test_client.get("/settings/keys").json()["keys"]
    tavily = next(key for key in listing if key["id"] == "tavily")
    assert tavily["set"] is True
    assert "tvly-test-123" not in tavily["masked"]


def test_setting_a_key_updates_live_settings(client) -> None:
    test_client, env_path = client
    test_client.put("/settings/keys/nebius", json={"value": "nkey-live"})
    text = Path(env_path).read_text(encoding="utf-8")
    assert "NEBIUS_API_KEY=nkey-live" in text


def test_setting_an_existing_key_overwrites_the_line(client) -> None:
    test_client, env_path = client
    test_client.put("/settings/keys/tavily", json={"value": "first"})
    test_client.put("/settings/keys/tavily", json={"value": "second"})
    text = Path(env_path).read_text(encoding="utf-8")
    assert "TAVILY_API_KEY=second" in text
    assert text.count("TAVILY_API_KEY=") == 1


def test_clearing_a_key_blanks_the_env_line(client) -> None:
    test_client, env_path = client
    test_client.put("/settings/keys/tavily", json={"value": "gone-soon"})
    response = test_client.delete("/settings/keys/tavily")
    assert response.status_code == 200
    text = Path(env_path).read_text(encoding="utf-8")
    assert "TAVILY_API_KEY=" in text and "gone-soon" not in text


def test_unknown_key_is_rejected(client) -> None:
    test_client, _env = client
    response = test_client.put("/settings/keys/not_a_key", json={"value": "x"})
    assert response.status_code == 404


def test_empty_value_is_rejected(client) -> None:
    test_client, _env = client
    response = test_client.put("/settings/keys/tavily", json={"value": "   "})
    assert response.status_code == 422


def test_uri_fields_can_be_set(client) -> None:
    test_client, env_path = client
    uri = "https://in03-abc.serverless.gcp-us-west1.cloud.zilliz.com"
    response = test_client.put("/settings/keys/zilliz_uri", json={"value": uri})
    assert response.status_code == 200
    assert response.json()["kind"] == "uri"
    assert f"ZILLIZ_URI={uri}" in Path(env_path).read_text(encoding="utf-8")
