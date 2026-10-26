"""Session memory: the transcript, the message buffer, and the rolling summary.

Three concerns, kept deliberately apart — this split is the whole design:

1. **Transcript** — every user and assistant turn, durably, in the record store. It is never
   trimmed. This is what the chat sidebar lists and what a reopened conversation replays.
2. **Message buffer** — the tail of recent turns that actually reaches the model, bounded by a
   character budget so one long session cannot blow the prompt.
3. **Summary** — a rolling synopsis of everything *older* than the buffer, regenerated when the
   unsummarised tail grows past a threshold. Turns leave the prompt without being forgotten.

Before this module existed the planner received exactly two messages — a system prompt and the
current request — so a follow-up such as "explain in detail" had no referent and the model had to
guess one from unrelated memories. Storing history and *sending* history are different problems;
this module solves both, separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.schemas import ChatMessageRecord, ConversationRecord, SessionTurn, new_id

logger = get_logger(__name__)

#: Store collections. Both are user-scoped by ``UserScopedStore`` in multi-user mode, so one
#: person's transcript is not another's.
CONVERSATIONS = "conversations"
MESSAGES = "messages"

ROLE_USER = "user"
ROLE_ASSISTANT = "dobot"

#: Safe in a URL path, a JSON key and (should it ever come to that) a filename.
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def normalise_id(raw: str) -> str:
    """Collapse a caller-supplied conversation id to something addressable, or ``""``."""
    return _UNSAFE_ID.sub("-", (raw or "").strip())[:64].strip("-")


def title_from(message: str) -> str:
    """The sidebar label: the first line of the first thing the user said."""
    stripped = (message or "").strip()
    if not stripped:
        return "New chat"
    line = " ".join(stripped.splitlines()[0].split())
    return f"{line[:60]}…" if len(line) > 60 else line


def _timestamp(raw: Any) -> datetime | None:
    """Best-effort parse of a stored ISO timestamp; ``None`` means "keep the original order"."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


@dataclass
class SessionBuffer:
    """What the model should be shown of the conversation so far."""

    summary: str = ""
    turns: list[SessionTurn] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.summary and not self.turns


class SessionService:
    """Owns conversation records, their messages, and the context policy over them."""

    def __init__(
        self,
        store: Any,
        *,
        settings: Settings | None = None,
        reasoner: Any = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        #: Optional; compaction degrades to "no summary yet" when it is absent or offline, which
        #: must never block a request.
        self.reasoner = reasoner

    # ------------------------------------------------------------------ transcript

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        cid = normalise_id(conversation_id)
        if not cid:
            return None
        doc = await self.store.get(CONVERSATIONS, cid)
        if not doc:
            return None
        try:
            return ConversationRecord.model_validate(doc)
        except Exception:  # noqa: BLE001 - a corrupt header must not break the sidebar
            return None

    async def list(self, limit: int = 50) -> list[ConversationRecord]:
        """Newest-first list of conversations that actually contain something."""
        conversations: list[ConversationRecord] = []
        for doc in await self.store.all(CONVERSATIONS, limit=500):
            try:
                record = ConversationRecord.model_validate(doc)
            except Exception:  # noqa: BLE001
                continue
            if record.message_count > 0:
                conversations.append(record)
        conversations.sort(key=lambda item: item.updated_at, reverse=True)
        return conversations[:limit]

    async def messages(self, conversation_id: str, limit: int = 500) -> list[ChatMessageRecord]:
        cid = normalise_id(conversation_id)
        if not cid:
            return []
        records: list[ChatMessageRecord] = []
        for doc in await self.store.all(MESSAGES, limit=5000):
            if doc.get("conversation_id") != cid:
                continue
            try:
                records.append(ChatMessageRecord.model_validate(doc))
            except Exception:  # noqa: BLE001 - skip a malformed turn, keep the transcript readable
                continue
        records.sort(key=lambda item: item.seq)
        return records[-limit:]

    async def record(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        task_id: str = "",
        status: str = "",
    ) -> ChatMessageRecord | None:
        """Append one durable turn and refresh the conversation header.

        Returns ``None`` for empty content — an unanswered placeholder is not a turn.
        """
        text = (content or "").strip()
        if not text:
            return None
        conversation = await self._ensure(conversation_id)
        now = datetime.now(UTC)
        record = ChatMessageRecord(
            conversation_id=conversation.id,
            seq=conversation.message_count,
            role=ROLE_ASSISTANT if role == ROLE_ASSISTANT else ROLE_USER,
            content=text[: int(self.settings.session_message_chars)],
            task_id=task_id,
            status=status,
            created_at=now,
        )
        await self.store.insert(MESSAGES, record.model_dump(mode="json"))
        patch: dict = {
            "message_count": conversation.message_count + 1,
            "updated_at": now.isoformat(),
        }
        if record.role == ROLE_USER and conversation.message_count == 0:
            patch["title"] = title_from(text)
        await self.store.update(CONVERSATIONS, conversation.id, patch)
        return record

    async def delete(self, conversation_id: str) -> bool:
        cid = normalise_id(conversation_id)
        if not cid:
            return False
        await self.store.delete_many(
            MESSAGES, lambda doc: doc.get("conversation_id") == cid
        )
        return bool(await self.store.delete(CONVERSATIONS, cid))

    async def seed(self, conversation_id: str, turns: list[Any]) -> int:
        """Adopt a locally cached transcript into the database — at most once.

        Older builds kept the thread in the browser only, so the first time a chat that predates the
        transcript store is opened there is nothing here to replay. Pushing the cache up closes that
        gap instead of silently dropping the user's history.

        Refuses to run when the conversation already holds messages: a transcript is append-only and
        re-seeding would duplicate every turn.
        """
        if not turns:
            return 0
        conversation = await self._ensure(conversation_id)
        if await self.messages(conversation.id):
            return 0
        now = datetime.now(UTC)
        inserted = 0
        for turn in turns:
            role = getattr(turn, "role", "") or ""
            content = str(getattr(turn, "content", "") or "").strip()
            if not content:
                continue
            record = ChatMessageRecord(
                conversation_id=conversation.id,
                seq=inserted,
                role=ROLE_ASSISTANT if role == ROLE_ASSISTANT else ROLE_USER,
                content=content[: int(self.settings.session_message_chars)],
                status=str(getattr(turn, "status", "") or ""),
                created_at=_timestamp(getattr(turn, "created_at", None)) or now,
            )
            await self.store.insert(MESSAGES, record.model_dump(mode="json"))
            inserted += 1
        if not inserted:
            return 0
        stamp = now
        for turn in reversed(turns):
            parsed = _timestamp(getattr(turn, "created_at", None))
            if parsed is not None:
                stamp = parsed
                break
        patch: dict = {"message_count": inserted, "updated_at": stamp.isoformat()}
        if conversation.message_count == 0 and conversation.title == "New chat":
            first_user = next(
                (
                    turn
                    for turn in turns
                    if (getattr(turn, "role", "") or "") != ROLE_ASSISTANT
                    and str(getattr(turn, "content", "") or "").strip()
                ),
                None,
            )
            if first_user is not None:
                patch["title"] = title_from(str(first_user.content))
        await self.store.update(CONVERSATIONS, conversation.id, patch)
        return inserted

    async def _ensure(self, conversation_id: str) -> ConversationRecord:
        cid = normalise_id(conversation_id) or normalise_id(new_id("conv"))
        existing = await self.get(cid)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        conversation = ConversationRecord(id=cid, created_at=now, updated_at=now)
        await self.store.insert(CONVERSATIONS, conversation.model_dump(mode="json"))
        return conversation

    # ------------------------------------------------------------------ buffer

    async def buffer(self, conversation_id: str) -> SessionBuffer:
        """The bounded view of a conversation: summary + the newest raw turns.

        Turns dropped here are not lost — they are still in the transcript, and once they are old
        enough the summary covers them.
        """
        conversation = await self.get(conversation_id)
        if conversation is None:
            return SessionBuffer()
        messages = await self.messages(conversation_id)
        pending = messages[conversation.summary_through :]
        turns = [
            SessionTurn(role=message.role, content=message.content) for message in pending
        ]
        turns = turns[-int(self.settings.session_history_turns) :]

        budget = int(self.settings.session_history_chars)
        kept: list[SessionTurn] = []
        used = 0
        for turn in reversed(turns):
            cost = len(turn.content) + 8  # "User: " / "Dobot: " plus the newline
            if kept and used + cost > budget:
                break
            kept.append(turn)
            used += cost
        kept.reverse()
        return SessionBuffer(summary=conversation.summary, turns=kept)

    async def compact(self, conversation_id: str) -> bool:
        """Fold the oldest unsummarised turns into the rolling summary.

        Best-effort by construction: an unreachable model means the summary simply stays short for
        another round, and the raw tail keeps carrying the context. Compaction must never be able to
        fail a request.
        """
        conversation = await self.get(conversation_id)
        if conversation is None:
            return False
        messages = await self.messages(conversation_id)
        pending = messages[conversation.summary_through :]
        threshold = int(self.settings.session_compact_after)
        if len(pending) < threshold:
            return False

        retain = int(self.settings.session_history_turns)
        fold = pending[: max(1, len(pending) - retain)]
        if not fold:
            return False
        if self.reasoner is None or not getattr(self.reasoner, "available", False):
            return False

        transcript = "\n".join(
            f"{'User' if message.role == ROLE_USER else 'Dobot'}: {message.content}"
            for message in fold
        )
        system = (
            "You compress conversation history for an AI assistant's context window. "
            "Summarise the turns below, keeping facts, decisions, names, preferences, open "
            "questions and anything a later turn would need. Drop pleasantries and narration. "
            f"Write only the summary, under {self.settings.session_summary_chars} characters."
        )
        if conversation.summary:
            system += (
                "\n\nExtend this existing summary rather than repeating it:\n"
                f"{conversation.summary}"
            )
        try:
            response = await self.reasoner.text(
                transcript, system=system, thinking=False, max_tokens=700
            )
        except Exception as exc:  # noqa: BLE001 - compaction is an optimisation, never a gate
            logger.info("session compaction skipped (%s)", exc)
            return False
        if response.degraded or not (response.content or "").strip():
            return False

        summary = response.content.strip()[: int(self.settings.session_summary_chars) * 2]
        await self.store.update(
            CONVERSATIONS,
            conversation.id,
            {
                "summary": summary,
                "summary_through": conversation.summary_through + len(fold),
            },
        )
        return True
