"""Conversations — the durable chat list, and the transcript behind each entry.

Storage and prompting are separate concerns (see ``app/core/sessions.py``): this endpoint serves the
*transcript*, which is complete and never trimmed, while the planner only ever receives the bounded
buffer built from it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import ServicesDep
from app.schemas import ChatMessageRecord, ConversationRecord

router = APIRouter(prefix="/conversations", tags=["conversations"])


class SeedMessage(BaseModel):
    role: str = "user"
    content: str = ""
    status: str = ""
    created_at: str | None = None


class SeedRequest(BaseModel):
    messages: list[SeedMessage] = Field(default_factory=list)


@router.get("", response_model=list[ConversationRecord])
async def list_conversations(services: ServicesDep, limit: int = Query(50, ge=1, le=200)) -> list[ConversationRecord]:
    """Newest-first list of every chat this user can reopen."""
    return await services.sessions.list(limit=limit)


@router.get("/{conversation_id}")
async def conversation(
    conversation_id: str,
    services: ServicesDep,
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    """One chat in full: the header plus every turn, oldest first.

    The client replays this to rebuild the thread, then keeps sending under the same id — which is
    what makes a past conversation continuable rather than merely readable.
    """
    conversation_record: ConversationRecord | None = await services.sessions.get(conversation_id)
    messages: list[ChatMessageRecord] = await services.sessions.messages(conversation_id, limit=limit)
    if conversation_record is None and not messages:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CONVERSATION_NOT_FOUND",
                "message": "no such conversation",
                "recoverable": False,
            },
        )
    return {
        "conversation": conversation_record,
        "messages": [message.model_dump(mode="json") for message in messages],
    }


@router.post("/{conversation_id}/messages")
async def seed_conversation(
    conversation_id: str,
    payload: SeedRequest,
    services: ServicesDep,
) -> dict:
    """Upload a transcript the browser was holding on its own.

    Only accepted while the conversation is still empty — a transcript is append-only, so seeding a
    conversation that already has turns would duplicate every one of them.
    """
    inserted = await services.sessions.seed(conversation_id, payload.messages)
    return {"inserted": inserted}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str, services: ServicesDep) -> dict:
    """Remove a chat and all of its turns. Tasks and memories are deliberately left alone."""
    if not await services.sessions.delete(conversation_id):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CONVERSATION_NOT_FOUND",
                "message": "no such conversation",
                "recoverable": False,
            },
        )
    return {"success": True, "deleted": conversation_id}
