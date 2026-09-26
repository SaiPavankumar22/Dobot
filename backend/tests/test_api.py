"""HTTP surface: chat, screen, research, tasks, automations, memory, security, activity."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_provider_posture(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["providers"]["nemotron"] == "not_configured"
    assert "nemotron" in body["degraded"]
    assert body["tools"] > 10


def test_root_lists_the_surface(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["name"] == "Dobot"
    assert any("chat" in endpoint for endpoint in body["endpoints"])


def test_tool_inventory_exposes_risk_floors(client: TestClient) -> None:
    body = client.get("/chat/tools").json()
    floors = {tool["name"]: tool["risk_floor"] for tool in body["tools"]}
    assert floors["fs_delete"] == "HIGH"
    assert floors["tavily_search"] == "LOW"


def test_chat_round_trip(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "hello there"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["answer"]
    assert body["plan"]["steps"] == []
    assert body["timeline"]


def test_chat_rejects_empty_message(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EMPTY_MESSAGE"


def test_shadow_mode_request_reports_a_plan_only(client: TestClient) -> None:
    response = client.post(
        "/chat", json={"message": "clean my downloads folder", "shadow": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "WAITING_USER"
    assert body["answer"].startswith("SHADOW MODE")


def test_screen_privacy_defaults_to_on_demand(client: TestClient) -> None:
    body = client.get("/screen/privacy").json()
    assert body["continuous_monitoring"] is False
    assert body["mode"] == "on_demand"


def test_screen_analyze_requires_an_image(client: TestClient) -> None:
    response = client.post("/screen/analyze", json={"question": "explain this"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_SCREEN_INPUT"


def test_security_status_is_honest_about_isolation(client: TestClient) -> None:
    body = client.get("/security/status").json()
    assert body["sandbox"]["provider"] == "local"
    assert body["sandbox"]["isolation"] == "none"
    assert body["continuous_monitoring"] is False
    assert body["policies"]
    assert body["require_confirmation_for"]


def test_kill_switch_endpoint(client: TestClient) -> None:
    body = client.post("/security/kill").json()
    assert body["killed"] == []


def test_memory_crud(client: TestClient) -> None:
    created = client.post(
        "/memory",
        json={"content": "The user prefers Python for backend work", "type": "preference", "importance": 0.7},
    ).json()
    assert created["id"]
    listed = client.get("/memory").json()
    assert any(record["id"] == created["id"] for record in listed)
    recalled = client.get("/memory/recall", params={"q": "Python backend"}).json()
    assert any(hit["memory"]["id"] == created["id"] for hit in recalled)
    assert client.delete(f"/memory/{created['id']}").json()["deleted"] is True
    missing = client.delete(f"/memory/{created['id']}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "MEMORY_NOT_FOUND"


def test_memory_stats(client: TestClient) -> None:
    body = client.get("/memory/stats").json()
    assert "total" in body
    assert body["store"]


def test_task_creation_and_listing(client: TestClient) -> None:
    created = client.post("/tasks", json={"title": "Research AI agents", "priority": "high"}).json()
    assert created["status"] == "PENDING"
    listed = client.get("/tasks").json()
    assert any(task["id"] == created["id"] for task in listed)
    assert client.get(f"/tasks/{created['id']}").json()["title"] == "Research AI agents"
    assert client.delete(f"/tasks/{created['id']}").json()["deleted"] is True
    assert client.get("/tasks/nope").status_code == 404


def test_automation_validation_and_lifecycle(client: TestClient) -> None:
    bad = client.post(
        "/automations", json={"name": "Weekly", "schedule": "not a cron", "task": "research"}
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "INVALID_SCHEDULE"

    created = client.post(
        "/automations",
        json={"name": "Weekly AI research", "schedule": "0 18 * * 5", "task": "Research new AI agent releases"},
    ).json()
    assert created["next_run_at"]
    paused = client.patch(f"/automations/{created['id']}", json={"status": "paused"}).json()
    assert paused["status"] == "paused"
    listed = client.get("/automations").json()
    assert any(item["id"] == created["id"] for item in listed)
    assert client.delete(f"/automations/{created['id']}").json()["deleted"] is True


def test_approval_flow_over_http(client: TestClient) -> None:
    import os
    from pathlib import Path

    folder = Path(os.environ["USERPROFILE"]) / "Downloads"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "throwaway.txt").write_text("x", encoding="utf-8")

    chat = client.post("/chat", json={"message": "delete all files in my downloads folder"}).json()
    assert chat["status"] == "WAITING_APPROVAL"
    pending = client.get("/approvals").json()
    assert pending
    approval_id = pending[0]["id"]
    resolved = client.post(f"/approvals/{approval_id}", json={"decision": "approve"}).json()
    assert resolved["status"] == "COMPLETED"
    assert not (folder / "throwaway.txt").exists()


def test_research_endpoint_degrades_without_tavily(client: TestClient) -> None:
    body = client.post("/research", json={"query": "multimodal agents", "depth": "quick"}).json()
    assert body["status"] == "completed"
    assert body["degraded"] is True
    assert body["sub_queries"]


def test_dashboard_summary(client: TestClient) -> None:
    body = client.get("/dashboard").json()
    assert "tasks" in body
    assert "providers" in body
    assert body["approvals"]["pending"] >= 0


def test_settings_and_onboarding(client: TestClient) -> None:
    settings_body = client.get("/settings").json()
    assert settings_body["models"]["primary"]
    providers = client.get("/settings/providers").json()
    assert providers["nemotron"] == "not_configured"
    onboarding = client.get("/settings/onboarding").json()
    assert onboarding["complete"] is False
    patched = client.patch("/settings", json={"shadow_mode": True}).json()
    assert patched["applied"]["shadow_mode"] is True
    assert client.patch("/settings", json={"nonsense": 1}).status_code == 422


def test_activity_and_trace(client: TestClient) -> None:
    client.post("/chat", json={"message": "hello"})
    activity = client.get("/activity", params={"limit": 20}).json()
    assert activity
    trace = client.get("/debug/trace", params={"limit": 20}).json()
    assert trace["lines"]
    assert trace["lines"][0].startswith("[")


def test_skills_endpoint(client: TestClient) -> None:
    listing = client.get("/skills").json()
    assert isinstance(listing, list)
    created = client.post(
        "/skills",
        json={
            "name": "test_skill",
            "description": "A skill created by the test suite",
            "body": "Do the thing.",
            "workflow": [{"tool": "fs_list", "params": {"path": "~"}}],
            "required_tools": ["fs_list"],
        },
    ).json()
    assert created["name"] == "test_skill"
    assert any(skill["name"] == "test_skill" for skill in client.get("/skills").json())
    assert client.delete("/skills/test_skill").json()["deleted"] is True
