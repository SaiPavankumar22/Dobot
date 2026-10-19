"""Multi-user architecture: accounts, per-user isolation, and the single-user default.

Every test here runs with ``MULTI_USER=true`` except the legacy checks, which prove the
old behaviour is untouched: no accounts, no token, one shared store. Nothing in this
file talks to a network provider.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def multi_settings(settings: Settings) -> Settings:
    settings.multi_user = True
    return settings


@pytest.fixture()
def client(multi_settings: Settings):
    with TestClient(create_app(multi_settings)) as test_client:
        yield test_client


def _register(client: TestClient, name: str, password: str = "correct horse battery", invite: str = "") -> dict:
    payload: dict = {"name": name, "password": password}
    if invite:
        payload["invite_code"] = invite
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _invite(client: TestClient, owner_token: str) -> str:
    response = client.post("/auth/invites", headers=_auth(owner_token))
    assert response.status_code == 200, response.text
    return response.json()["invite_code"]


def _pair(client: TestClient) -> tuple[dict, dict]:
    """An owner and a member, ready to use."""
    alice = _register(client, "alice")
    bob = _register(client, "bob", invite=_invite(client, alice["token"]))
    return alice, bob


# ------------------------------------------------------------------ accounts


def test_first_account_is_the_owner(client: TestClient) -> None:
    body = _register(client, "ada")
    assert body["user"]["role"] == "owner"
    assert body["token"].startswith("dobot_u_")


def test_second_account_needs_an_invite_and_becomes_a_member(client: TestClient) -> None:
    owner = _register(client, "ada")
    refused = client.post("/auth/register", json={"name": "bob", "password": "correct horse battery"})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "INVITE_REQUIRED"

    code = _invite(client, owner["token"])
    body = _register(client, "bob", invite=code)
    assert body["user"]["role"] == "member"

    # The invite is single-use: the second attempt is rejected, not silently accepted.
    reused = client.post(
        "/auth/register",
        json={"name": "carol", "password": "correct horse battery", "invite_code": code},
    )
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "REGISTRATION_REJECTED"


def test_login_returns_a_working_token(client: TestClient) -> None:
    _register(client, "ada")
    logged_in = client.post("/auth/login", json={"name": "ADA", "password": "correct horse battery"})
    assert logged_in.status_code == 200
    token = logged_in.json()["token"]
    me = client.get("/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["user"]["name"].lower() == "ada"


def test_bad_password_and_unknown_user_are_401(client: TestClient) -> None:
    _register(client, "ada")
    bad = client.post("/auth/login", json={"name": "ada", "password": "wrong password here"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "BAD_CREDENTIALS"


def test_only_owner_creates_invites(client: TestClient) -> None:
    alice, bob = _pair(client)
    denied = client.post("/auth/invites", headers=_auth(bob["token"]))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "OWNER_ONLY"


# ------------------------------------------------------------------ gates


def test_anonymous_requests_are_refused_in_multi_user(client: TestClient) -> None:
    _register(client, "ada")
    assert client.get("/dashboard").status_code == 401
    assert client.get("/memory").status_code == 401
    # The way in stays open.
    assert client.get("/auth/status").status_code == 200
    assert client.post("/auth/login", json={"name": "x", "password": "y"}).status_code == 401


def test_forged_and_stale_tokens_are_refused(client: TestClient) -> None:
    _register(client, "ada")
    forged = client.get("/dashboard", headers=_auth("dobot_u_user_deadbeef_notreal"))
    assert forged.status_code == 401
    assert forged.json()["error"]["code"] == "INVALID_USER_TOKEN"
    # A non-user token with no operator gate configured is also refused.
    assert client.get("/dashboard", headers=_auth("sekrit")).status_code == 401


# ------------------------------------------------------------------ isolation


def test_two_users_cannot_see_each_others_memory_and_tasks(client: TestClient) -> None:
    alice, bob = _pair(client)

    created = client.post(
        "/memory", json={"content": "Alice's private plan"}, headers=_auth(alice["token"])
    )
    assert created.status_code == 200
    memory_id = created.json()["id"]

    alice_sees = client.get("/memory", headers=_auth(alice["token"])).json()
    assert any(item["id"] == memory_id for item in alice_sees)

    bob_sees = client.get("/memory", headers=_auth(bob["token"])).json()
    assert all(item["id"] != memory_id for item in bob_sees), "Bob read Alice's memory"

    # Bob cannot delete it either — a foreign id is a 404, not a silent success.
    assert client.delete(f"/memory/{memory_id}", headers=_auth(bob["token"])).status_code == 404
    assert client.delete(f"/memory/{memory_id}", headers=_auth(alice["token"])).status_code == 200

    task = client.post("/tasks", json={"title": "Alice task"}, headers=_auth(alice["token"])).json()
    bob_tasks = client.get("/tasks", headers=_auth(bob["token"])).json()
    assert all(item["id"] != task["id"] for item in bob_tasks), "Bob read Alice's tasks"


def test_automations_are_owner_scoped_including_mutations(client: TestClient) -> None:
    alice, bob = _pair(client)

    record = client.post(
        "/automations",
        json={"name": "alice digest", "schedule": "0 9 * * 1", "task": "summarise my inbox"},
        headers=_auth(alice["token"]),
    ).json()
    assert record["user_id"] == alice["user"]["id"]

    bob_list = client.get("/automations", headers=_auth(bob["token"])).json()
    assert all(item["id"] != record["id"] for item in bob_list), "Bob sees Alice's automation"

    # Mutations by the non-owner are refusals, not no-ops on someone else's schedule.
    assert client.delete(f"/automations/{record['id']}", headers=_auth(bob["token"])).json()["deleted"] is False
    assert client.patch(
        f"/automations/{record['id']}", json={"status": "paused"}, headers=_auth(bob["token"])
    ).status_code == 404
    assert client.post(f"/automations/{record['id']}/run", headers=_auth(bob["token"])).status_code == 404

    # The owner still has full control.
    alice_list = client.get("/automations", headers=_auth(alice["token"])).json()
    assert any(item["id"] == record["id"] for item in alice_list)
    assert client.delete(f"/automations/{record['id']}", headers=_auth(alice["token"])).json()["deleted"] is True


def test_legacy_local_automations_stay_visible_to_every_user(client: TestClient) -> None:
    """Records that predate accounts (user_id='local') belong to the deployment, not a person."""
    alice, bob = _pair(client)
    services = client.app.state.services

    async def seed() -> None:
        base = getattr(services.store, "_base", services.store)
        from app.schemas import AutomationKind, AutomationRecord

        record = AutomationRecord(name="legacy digest", schedule="0 9 * * 1", prompt="old habit",
                                  kind=AutomationKind.CRON, status="paused", user_id="local")
        await base.insert("automations", record.model_dump(mode="json"))

    client.portal.call(seed)

    for token in (alice["token"], bob["token"]):
        names = [item["name"] for item in client.get("/automations", headers=_auth(token)).json()]
        assert "legacy digest" in names


def test_per_user_api_keys_never_leave_their_scope(client: TestClient) -> None:
    alice, bob = _pair(client)

    put = client.put(
        "/settings/keys/nebius", json={"value": "alice-key-123"}, headers=_auth(alice["token"])
    )
    assert put.status_code == 200
    assert put.json()["persisted"] == "user-scoped"

    alice_keys = client.get("/settings/keys", headers=_auth(alice["token"])).json()["keys"]
    own = next(k for k in alice_keys if k["id"] == "nebius")
    assert own["set"] and own["source"] == "user"
    assert "alice-key-123" not in own["masked"]  # presence, never the value

    bob_keys = client.get("/settings/keys", headers=_auth(bob["token"])).json()["keys"]
    bob_nebius = next(k for k in bob_keys if k["id"] == "nebius")
    assert bob_nebius["source"] != "user", "Bob sees Alice's key"

    # Deleting her key removes only hers; the deployment's .env is never written by this path.
    cleared = client.delete("/settings/keys/nebius", headers=_auth(alice["token"]))
    assert cleared.status_code == 200
    alice_keys = client.get("/settings/keys", headers=_auth(alice["token"])).json()["keys"]
    assert next(k for k in alice_keys if k["id"] == "nebius")["source"] != "user"


def test_event_stream_is_carved_per_user(client: TestClient) -> None:
    """Each user's /events carries their task events plus system ones — never anyone else's."""
    alice, bob = _pair(client)

    # End-to-end: the HTTP middleware scopes the request, the orchestrator emits,
    # the bus stamps the event — no synthetic shortcuts.
    assert client.post(
        "/chat", json={"message": "ALICE-MARKER plan my week"}, headers=_auth(alice["token"])
    ).status_code == 200
    assert client.post(
        "/chat", json={"message": "BOB-MARKER refactor the parser"}, headers=_auth(bob["token"])
    ).status_code == 200

    alice_events = [e["message"] for e in client.get("/events", headers=_auth(alice["token"])).json()["events"]]
    bob_events = [e["message"] for e in client.get("/events", headers=_auth(bob["token"])).json()["events"]]

    assert any("ALICE-MARKER" in m for m in alice_events)
    assert not any("BOB-MARKER" in m for m in alice_events), "Alice receives Bob's events"
    assert any("BOB-MARKER" in m for m in bob_events)
    assert not any("ALICE-MARKER" in m for m in bob_events), "Bob receives Alice's events"


def test_websocket_requires_a_token_and_filters_the_replay(client: TestClient) -> None:
    alice, bob = _pair(client)
    client.post("/chat", json={"message": "ALICE-MARKER hello"}, headers=_auth(alice["token"]))
    client.post("/chat", json={"message": "BOB-MARKER hello"}, headers=_auth(bob["token"]))

    # Anonymous sockets are refused in multi-user mode rather than upgraded to all-seeing.
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws") as stream:
            stream.receive_json()
    assert refused.value.code == 4401

    with client.websocket_connect(f"/ws?access_token={alice['token']}") as stream:
        hello = stream.receive_json()
    replay = [event["message"] for event in hello["replay"]]
    assert any("ALICE-MARKER" in m for m in replay)
    assert not any("BOB-MARKER" in m for m in replay), "Alice's WS replay carries Bob's events"


# ------------------------------------------------------------------ legacy


def test_single_user_mode_is_unchanged(settings: Settings) -> None:
    """MULTI_USER off: no accounts, no auth, the shared store everyone had before."""
    assert settings.multi_user is False
    with TestClient(create_app(settings)) as plain:
        # Accounts don't exist here: registering says so instead of silently working.
        refused = plain.post("/auth/register", json={"name": "x", "password": "long enough password"})
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "MULTI_USER_DISABLED"
        assert plain.get("/dashboard").status_code == 200
        created = plain.post("/memory", json={"content": "shared note"}).json()
        assert created["content"] == "shared note"
        assert plain.get("/memory").json()[0]["id"] == created["id"]
        with plain.websocket_connect("/ws") as stream:
            assert stream.receive_json()["type"] == "hello"
