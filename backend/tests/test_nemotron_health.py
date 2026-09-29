"""Nemotron health: a rejected key is reported as such, and a newly saved key is re-checked."""

from __future__ import annotations

import httpx

from app.agents.nemotron import NemotronClient, Reasoner
from app.config import Settings


def _client(settings: Settings, statuses: list[int]) -> tuple[NemotronClient, list[int]]:
    """A client whose /models probe answers with each status in turn; records every probe."""
    object.__setattr__(settings, "nebius_api_key", "test-key")
    client = NemotronClient(settings)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(status)
        return httpx.Response(status, json={"data": []})

    def build() -> httpx.AsyncClient:
        if client._client is None:
            client._client = httpx.AsyncClient(
                base_url="https://nebius.test/v1", transport=httpx.MockTransport(handler)
            )
        return client._client

    client._http = build  # type: ignore[method-assign]
    return client, calls


async def test_rejected_key_is_reported_as_rejected(settings: Settings) -> None:
    client, _ = _client(settings, [401])
    assert await client.health() is False
    assert client.key_rejected is True


async def test_saving_a_key_clears_a_cached_failure(settings: Settings) -> None:
    client, calls = _client(settings, [401, 200])
    assert await client.health() is False
    assert await client.health() is False
    assert len(calls) == 1  # a recent failure is cached, not re-probed on every status poll

    client.reset_http()  # what PUT /settings/keys does after persisting a new key
    assert await client.health() is True
    assert client.key_rejected is False
    assert len(calls) == 2


async def test_failed_probe_is_retried_after_the_retry_window(settings: Settings, monkeypatch) -> None:
    client, calls = _client(settings, [503, 200])
    assert await client.health() is False
    monkeypatch.setattr(client, "_checked_at", client._checked_at - 3600)
    assert await client.health() is True
    assert len(calls) == 2


async def test_auth_failure_on_a_live_call_invalidates_a_cached_success(settings: Settings) -> None:
    object.__setattr__(settings, "nebius_api_key", "test-key")
    reasoner = Reasoner(settings)
    reasoner.client._reachable = True

    async def refuse(*_args, **_kwargs):
        request = httpx.Request("POST", "https://nebius.test/v1/chat/completions")
        raise httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))

    reasoner.client.chat = refuse  # type: ignore[method-assign]
    response = await reasoner.text("hello")
    assert response.degraded is True
    assert reasoner.key_rejected is True
    assert await reasoner.health() is False
