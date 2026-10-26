"""Conversation transcripts, the bounded message buffer, and the rolling session summary.

The defect these guard against: the planner used to receive a system prompt plus the current
request and nothing else, so "explain in detail" had no referent, and the only history the product
kept lived in the browser. Storing history and sending history are separate problems, so they get
separate assertions here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.nemotron import ModelResponse, Reasoner
from app.core.planner import Planner
from app.main import create_app
from app.schemas import ChatRequest, ContextBundle, TaskStatus
from app.tools.registry import default_registry

ROLES = ("user", "dobot")


def _turns(count: int, body: str = "turn") -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            role=ROLES[index % 2],
            content=f"{body} {index}",
            status="COMPLETED" if index % 2 else "",
            created_at=None,
        )
        for index in range(count)
    ]


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


# -------------------------------------------------------------------------- transcript


@pytest.mark.asyncio
async def test_turns_are_durable_ordered_and_title_the_conversation(services) -> None:
    await services.sessions.record("conv-a", "user", "what is the capital of India?")
    await services.sessions.record("conv-a", "dobot", "New Delhi.", status="COMPLETED")

    conversations = await services.sessions.list()
    assert [item.id for item in conversations] == ["conv-a"]
    assert conversations[0].title == "what is the capital of India?"
    assert conversations[0].message_count == 2

    messages = await services.sessions.messages("conv-a")
    assert [message.role for message in messages] == ["user", "dobot"]
    assert [message.seq for message in messages] == [0, 1]
    assert messages[1].status == "COMPLETED"


@pytest.mark.asyncio
async def test_a_request_and_its_answer_are_both_recorded(services) -> None:
    response = await services.orchestrator.submit(
        ChatRequest(message="hello there", conversation_id="conv-run")
    )
    assert response.status is TaskStatus.COMPLETED

    messages = await services.sessions.messages("conv-run")
    assert [message.role for message in messages] == ["user", "dobot"]
    assert messages[0].content == "hello there"
    assert messages[1].content == response.answer
    assert messages[1].status == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_two_conversations_do_not_mix(services) -> None:
    await services.sessions.record("conv-one", "user", "first chat")
    await services.sessions.record("conv-two", "user", "second chat")

    assert [m.content for m in await services.sessions.messages("conv-one")] == ["first chat"]
    assert {item.id for item in await services.sessions.list()} == {"conv-one", "conv-two"}


@pytest.mark.asyncio
async def test_empty_content_is_not_a_turn(services) -> None:
    assert await services.sessions.record("conv-empty", "user", "   ") is None
    assert await services.sessions.messages("conv-empty") == []


# ------------------------------------------------------------------------- buffering


@pytest.mark.asyncio
async def test_history_reaches_the_prompt(services) -> None:
    await services.sessions.record("conv-ctx", "user", "IPO means Initial Public Offering")
    await services.sessions.record("conv-ctx", "dobot", "An IPO is a company's first share sale.")

    request = services.context_request_factory(
        message="explain in detail", conversation_id="conv-ctx"
    )
    bundle = await services.context.build(request)

    assert [turn.content for turn in bundle.history][0].startswith("IPO means")
    rendered = bundle.as_prompt_context()
    assert "RECENT TURNS IN THIS CONVERSATION" in rendered
    # The exact thing the model must be able to resolve "it" against.
    assert "IPO means Initial Public Offering" in rendered


@pytest.mark.asyncio
async def test_a_first_turn_in_a_new_conversation_has_no_history(services) -> None:
    request = services.context_request_factory(message="hello", conversation_id="conv-new")
    bundle = await services.context.build(request)
    assert bundle.history == []
    assert bundle.session_summary == ""
    assert "RECENT TURNS" not in bundle.as_prompt_context()


@pytest.mark.asyncio
async def test_buffer_respects_the_turn_and_character_budget(services) -> None:
    for index in range(40):
        role = ROLES[index % 2]
        await services.sessions.record("conv-long", role, f"turn {index} " + "x" * 400)

    buffer = await services.sessions.buffer("conv-long")
    settings = services.settings
    assert len(buffer.turns) <= settings.session_history_turns
    assert sum(len(turn.content) for turn in buffer.turns) <= settings.session_history_chars
    # Newest turns survive; the transcript itself is never trimmed.
    assert buffer.turns[-1].content.startswith("turn 39")
    assert len(await services.sessions.messages("conv-long")) == 40


@pytest.mark.asyncio
async def test_summarised_turns_leave_the_buffer_but_not_the_transcript(services) -> None:
    class _Summariser:
        available = True

        async def text(self, prompt, *, system="", max_tokens=0, thinking=False):
            return ModelResponse(
                content="You asked three questions about IPOs and I explained each one.",
                model="stub",
            )

    services.sessions.reasoner = _Summariser()
    for index in range(24):
        await services.sessions.record("conv-sum", ROLES[index % 2], f"topic {index}")

    assert await services.sessions.compact("conv-sum") is True

    conversation = await services.sessions.get("conv-sum")
    assert conversation.summary_through > 0
    assert "IPOs" in conversation.summary
    # Everything still exists …
    assert len(await services.sessions.messages("conv-sum")) == 24
    # … but only the unsummarised tail is sent, under a summary heading.
    buffer = await services.sessions.buffer("conv-sum")
    assert buffer.summary
    assert len(buffer.turns) <= services.settings.session_history_turns
    oldest_sent = buffer.turns[0].content
    assert int(oldest_sent.split()[1]) >= conversation.summary_through
    rendered = ContextBundle(
        session_summary=buffer.summary, history=buffer.turns
    ).as_prompt_context()
    assert "EARLIER IN THIS CONVERSATION" in rendered


@pytest.mark.asyncio
async def test_compaction_degrades_silently_when_there_is_no_model(services) -> None:
    services.sessions.reasoner = None
    for index in range(24):
        await services.sessions.record("conv-quiet", ROLES[index % 2], f"line {index}")

    assert await services.sessions.compact("conv-quiet") is False
    conversation = await services.sessions.get("conv-quiet")
    assert conversation.summary == ""
    assert conversation.summary_through == 0
    # The raw tail still carries the context, which is the point of degrading this way.
    assert len((await services.sessions.buffer("conv-quiet")).turns) > 0


# --------------------------------------------------------------------------- seeding


@pytest.mark.asyncio
async def test_a_cached_transcript_is_adopted_exactly_once(services) -> None:
    turns = _turns(4)
    assert await services.sessions.seed("conv-seed", turns) == 4
    assert await services.sessions.seed("conv-seed", turns) == 0

    messages = await services.sessions.messages("conv-seed")
    assert len(messages) == 4
    assert messages[0].role == "user"
    conversation = await services.sessions.get("conv-seed")
    assert conversation.message_count == 4
    assert conversation.title.startswith("turn 0")


@pytest.mark.asyncio
async def test_delete_removes_the_conversation_and_every_turn(services) -> None:
    await services.sessions.record("conv-gone", "user", "keep me?")
    assert await services.sessions.delete("conv-gone") is True
    assert await services.sessions.messages("conv-gone") == []
    assert await services.sessions.get("conv-gone") is None
    assert await services.sessions.delete("conv-gone") is False


# ------------------------------------------------------------------------------ API


def test_chat_then_reopen_then_delete_round_trip(client: TestClient) -> None:
    sent = client.post("/chat", json={"message": "hello there", "conversation_id": "conv-api"})
    assert sent.status_code == 200
    assert sent.json()["status"] == "COMPLETED"

    listed = client.get("/conversations")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == ["conv-api"]
    assert listed.json()[0]["message_count"] == 2

    detail = client.get("/conversations/conv-api")
    assert detail.status_code == 200
    body = detail.json()
    assert [message["role"] for message in body["messages"]] == ["user", "dobot"]
    assert body["messages"][0]["content"] == "hello there"
    # Reopening under the same id is what makes a past chat continuable, not just readable.
    again = client.post("/chat", json={"message": "and again", "conversation_id": "conv-api"})
    assert again.status_code == 200
    assert len(client.get("/conversations/conv-api").json()["messages"]) == 4

    assert client.get("/conversations/no-such-chat").status_code == 404
    assert client.delete("/conversations/conv-api").status_code == 200
    assert client.get("/conversations").json() == []


def test_cached_transcript_is_uploaded_once(client: TestClient) -> None:
    payload = {
        "messages": [
            {"role": "user", "content": "an old turn"},
            {"role": "dobot", "content": "an old answer", "status": "COMPLETED"},
        ]
    }
    first = client.post("/conversations/conv-upload/messages", json=payload)
    assert first.status_code == 200
    assert first.json() == {"inserted": 2}

    # A transcript is append-only: seeding a conversation that already has turns would duplicate
    # every one of them.
    assert client.post("/conversations/conv-upload/messages", json=payload).json() == {"inserted": 0}
    assert len(client.get("/conversations/conv-upload").json()["messages"]) == 2


# -------------------------------------------------------------- degraded is not success


@pytest.fixture()
def offline_planner(settings) -> Planner:
    return Planner(reasoner=Reasoner(settings), registry=default_registry(), settings=settings)


class _FailingReasoner:
    """A key is configured and the call is made — the provider simply does not answer."""

    available = True

    async def chat(self, messages, **kwargs):
        return ModelResponse(content="", model="", degraded=True)


@pytest.mark.asyncio
async def test_keyless_offline_mode_is_a_finished_run(offline_planner: Planner) -> None:
    """Running with no key is the documented mode: the rules answer and that counts as done."""
    plan = await offline_planner.plan("hello there", ContextBundle())
    assert plan.degraded is True
    assert plan.unanswered is False


@pytest.mark.asyncio
async def test_a_configured_model_that_cannot_answer_is_unanswered(offline_planner: Planner) -> None:
    """The screenshot case: the provider fails and the outage report must not read as success."""
    offline_planner.reasoner = _FailingReasoner()
    plan = await offline_planner.plan("explain in detail", ContextBundle())
    assert plan.degraded is True
    assert plan.unanswered is True


@pytest.mark.asyncio
async def test_a_failed_model_that_the_rules_can_still_handle_is_not_unanswered(
    offline_planner: Planner,
) -> None:
    """\"remember that …\" needs no model: the rule executes it, so the run genuinely completed."""
    offline_planner.reasoner = _FailingReasoner()
    plan = await offline_planner.plan("remember that my editor is Neovim", ContextBundle())
    assert plan.steps
    assert plan.unanswered is False
