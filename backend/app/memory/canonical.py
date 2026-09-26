"""The canonical ledger - the handful of things Dobot should never have to look up.

Retrieval has a failure mode that is easy to miss: the more memories you store, the less reliably the
important ones come back. Your name is not a search result. Neither is "we agreed not to touch the
client's repository" or "the deploy command is X". If those have to win a similarity contest against a
million episode summaries, some days they won't, and the assistant will look forgetful about the one
thing it must never forget.

So this module keeps a *small, capped, always-injected* layer of ground truth that bypasses search
entirely. Its rules are the interesting part:

* **Restating a fact strengthens it.** Confirmation count grows, confidence follows logarithmically.
* **Contradicting a well-established fact does not silently overwrite it.** The new statement is
  recorded as a candidate and must be repeated before it wins, so one ambiguous sentence cannot erase
  something you have stated ten times.
* **Superseding keeps history.** The old belief is retired with its full statement trail, never deleted.
* **The ledger is capped.** Only facts that keep earning their place stay resident; the rest are
  retired to normal memory rather than dropped.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.logging_setup import get_logger

logger = get_logger(__name__)

COLLECTION = "canonical"
STATEMENT_KEEP = 20
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalise(text: str) -> str:
    """Case- and punctuation-insensitive form, used to recognise the same statement again."""
    lowered = _PUNCT.sub(" ", (text or "").lower())
    return _WS.sub(" ", lowered).strip()


def derive_key(text: str, *, explicit: str = "") -> str:
    """An explicit key groups revisions of one belief; otherwise the statement keys itself."""
    source = explicit or text
    return normalise(source)[:120] or "unnamed"


def confidence_for(reinforcements: int) -> float:
    """Confidence grows logarithmically with corroboration and saturates below certainty.

    Fitted so one statement is worth little and repetition is worth a lot:
    ``1 -> 0.35``, ``2 -> 0.55``, ``3 -> 0.63``, ``5 -> 0.79``, ``10 -> 0.95``, never reaching 1.0.
    A system that reports 100% certainty about a remembered sentence is overclaiming.
    """
    if reinforcements <= 0:
        return 0.2
    return round(min(0.99, 1.0 - 0.8643 * math.exp(-0.285 * reinforcements)), 3)


class CanonicalFact(BaseModel):
    id: str
    key: str
    content: str
    confidence: float = 0.5
    reinforcements: int = 1
    pinned: bool = False
    status: str = "active"  # active | retired
    source: str = "chat"
    tags: list[str] = Field(default_factory=list)
    statements: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def line(self, *, show_confidence: bool = True) -> str:
        suffix = ""
        if show_confidence:
            suffix = f"  [{self.confidence:.2f} confidence, stated {self.reinforcements}x"
            suffix += ", pinned]" if self.pinned else "]"
        return f"- {self.content}{suffix}"


class CanonicalLedger:
    """Storage-backed ledger. Every method is safe to call on a cold or unavailable store."""

    def __init__(self, store: Any, *, limit: int = 40, promote_after: int = 3) -> None:
        self.store = store
        self.limit = max(4, limit)
        self.promote_after = max(2, promote_after)

    # ------------------------------------------------------------------ reads

    async def _all(self) -> list[CanonicalFact]:
        facts: list[CanonicalFact] = []
        try:
            docs = await self.store.all(COLLECTION, limit=2000)
        except Exception as exc:  # noqa: BLE001 - the ledger is an optimisation, never a dependency
            logger.warning("could not read the canonical ledger (%s)", exc)
            return []
        for doc in docs:
            try:
                facts.append(CanonicalFact.model_validate(doc))
            except Exception:  # noqa: BLE001
                continue
        return facts

    async def active(self, *, limit: int | None = None) -> list[CanonicalFact]:
        facts = [fact for fact in await self._all() if fact.status == "active"]
        facts.sort(key=lambda fact: (not fact.pinned, -fact.confidence, -fact.reinforcements))
        return facts[: limit or self.limit]

    async def get(self, fact_id: str) -> CanonicalFact | None:
        try:
            doc = await self.store.get(COLLECTION, fact_id)
        except Exception:  # noqa: BLE001
            return None
        if not doc:
            return None
        try:
            return CanonicalFact.model_validate(doc)
        except Exception:  # noqa: BLE001
            return None

    async def find(self, key: str) -> CanonicalFact | None:
        target = normalise(key)
        for fact in await self._all():
            if fact.status == "active" and fact.key == target:
                return fact
        return None

    # ------------------------------------------------------------------ writes

    async def _save(self, fact: CanonicalFact) -> CanonicalFact:
        try:
            await self.store.insert(COLLECTION, fact.as_dict())
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist canonical fact %s (%s)", fact.id, exc)
        return fact

    async def state(
        self,
        content: str,
        *,
        key: str = "",
        source: str = "chat",
        importance: float = 0.6,
        pinned: bool = False,
        tags: list[str] | None = None,
    ) -> CanonicalFact:
        """State a fact. Reinforces it if known, revises it if the change is earned, else adds it."""
        text = " ".join((content or "").split())
        if not text:
            raise ValueError("a canonical fact needs content")
        derived = derive_key(text, explicit=key)
        existing = await self.find(derived)
        now = utcnow()

        if existing is None:
            fact = CanonicalFact(
                id=f"cf_{abs(hash(derived)) & 0xFFFFFFFF:08x}_{int(now.timestamp())}",
                key=derived,
                content=text[:600],
                confidence=confidence_for(1),
                reinforcements=1,
                pinned=pinned,
                source=source,
                tags=list(tags or []),
                statements=[{"at": now.isoformat(), "text": text[:600], "source": source}],
            )
            await self._save(fact)
            await self._enforce_cap()
            return fact

        existing.last_seen_at = now
        existing.updated_at = now
        if pinned:
            existing.pinned = True

        if normalise(existing.content) == normalise(text):
            # Restating: strengthen, do not revise.
            existing.reinforcements += 1
            existing.confidence = max(existing.confidence, confidence_for(existing.reinforcements))
            existing.statements.append({"at": now.isoformat(), "text": text[:600], "source": source})
            existing.statements = existing.statements[-STATEMENT_KEEP:]
            return await self._save(existing)

        # A different statement for the same key.
        strong_belief = existing.reinforcements >= self.promote_after
        if not strong_belief:
            return await self._revise(existing, text, source=source, reason="superseded by a newer statement")

        candidate = next(
            (item for item in existing.candidates if item.get("normalised") == normalise(text)), None
        )
        if candidate is None:
            existing.candidates.append(
                {
                    "text": text[:600],
                    "normalised": normalise(text),
                    "count": 1,
                    "source": source,
                    "at": now.isoformat(),
                }
            )
            existing.candidates = existing.candidates[-10:]
            return await self._save(existing)

        candidate["count"] = int(candidate.get("count", 1)) + 1
        candidate["at"] = now.isoformat()
        if candidate["count"] >= self.promote_after:
            return await self._revise(
                existing,
                text,
                source=source,
                reason=f"superseded after {candidate['count']} consistent statements",
            )
        return await self._save(existing)

    async def _revise(
        self, fact: CanonicalFact, text: str, *, source: str, reason: str
    ) -> CanonicalFact:
        """Supersede the active content, keeping the old belief in history rather than deleting it."""
        now = utcnow()
        fact.history.append(
            {
                "text": fact.content,
                "at": now.isoformat(),
                "reason": reason,
                "reinforcements": fact.reinforcements,
                "confidence": fact.confidence,
            }
        )
        fact.history = fact.history[-STATEMENT_KEEP:]
        fact.content = text[:600]
        fact.reinforcements = 1
        fact.confidence = confidence_for(1)
        fact.candidates = []
        fact.statements.append({"at": now.isoformat(), "text": text[:600], "source": source, "revision": reason})
        fact.statements = fact.statements[-STATEMENT_KEEP:]
        fact.updated_at = now
        fact.source = source
        logger.info("canonical fact %s revised: %s", fact.id, reason)
        return await self._save(fact)

    async def pin(self, fact_id: str, *, pinned: bool = True) -> CanonicalFact | None:
        fact = await self.get(fact_id)
        if fact is None:
            return None
        fact.pinned = pinned
        fact.updated_at = utcnow()
        return await self._save(fact)

    async def forget(self, fact_id: str) -> bool:
        try:
            return await self.store.delete(COLLECTION, fact_id)
        except Exception:  # noqa: BLE001
            return False

    async def retire(self, fact_id: str, *, reason: str = "retired") -> CanonicalFact | None:
        """Move a fact out of the always-injected set without destroying its trail."""
        fact = await self.get(fact_id)
        if fact is None:
            return None
        fact.status = "retired"
        fact.updated_at = utcnow()
        fact.history.append({"text": fact.content, "at": utcnow().isoformat(), "reason": reason})
        return await self._save(fact)

    async def _enforce_cap(self) -> int:
        """Retire the weakest unpinned facts when the resident set grows past the cap."""
        facts = [fact for fact in await self._all() if fact.status == "active"]
        if len(facts) <= self.limit:
            return 0
        pinned = [fact for fact in facts if fact.pinned]
        others = sorted(
            (fact for fact in facts if not fact.pinned),
            key=lambda fact: (fact.confidence, fact.reinforcements, fact.last_seen_at),
        )
        room = max(0, self.limit - len(pinned))
        doomed = others[: max(0, len(others) - room)]
        for fact in doomed:
            await self.retire(fact.id, reason="retired: the resident ledger is full")
        return len(doomed)

    # ------------------------------------------------------------------ promotion

    async def consolidate_from(
        self, records: Any, *, repeat_counts: dict[str, int] | None = None
    ) -> dict[str, int]:
        """Promote facts that have been stated repeatedly, and retire what no longer holds.

        ``records`` is the memory manager's record list; repetition is counted by content, and only
        statements seen at least twice are considered - a single mention is not yet a belief.

        ``repeat_counts`` overrides the repetition count for a key. The consolidation pass merges
        duplicates before calling this, so without the override every count would read 1 and nothing
        would ever promote; the pass therefore captures the counts before it merges.
        """
        counts: dict[str, dict[str, Any]] = {}
        for record in records:
            try:
                if getattr(record, "type", None) is None:
                    continue
                if str(getattr(record.type, "value", record.type)) not in {"fact", "preference", "project", "person"}:
                    continue
            except Exception:  # noqa: BLE001
                continue
            content = str(getattr(record, "content", "")).strip()
            if not content or len(content) > 400:
                continue
            key = normalise(content)
            bucket = counts.setdefault(key, {"content": content, "count": 0, "importance": 0.0})
            bucket["count"] += 1
            bucket["importance"] = max(bucket["importance"], float(getattr(record, "importance", 0.5)))

        if repeat_counts:
            for key, bucket in counts.items():
                bucket["count"] = max(bucket["count"], int(repeat_counts.get(key, 0)))

        promoted = 0
        reinforced = 0
        for key, bucket in counts.items():
            if bucket["count"] < 2:
                continue
            existing = await self.find(key)
            if existing is None:
                if bucket["count"] >= self.promote_after or bucket["importance"] >= 0.8:
                    await self.state(
                        bucket["content"],
                        source="consolidation",
                        importance=float(bucket["importance"]),
                    )
                    promoted += 1
                continue
            if normalise(existing.content) == key:
                for _ in range(bucket["count"] - 1):
                    await self.state(existing.content, source="consolidation")
                reinforced += 1
        return {"promoted": promoted, "reinforced": reinforced}

    # ------------------------------------------------------------------ rendering

    async def render(self, *, max_chars: int = 1400, show_confidence: bool = True) -> str:
        """The block injected into every prompt. Returns '' when the ledger is empty."""
        facts = await self.active()
        if not facts:
            return ""
        lines = ["[CANONICAL FACTS - long-standing, treat as known without searching]"]
        used = len(lines[0])
        for fact in facts:
            line = fact.line(show_confidence=show_confidence)
            if used + len(line) + 1 > max_chars:
                lines.append(f"…(+{len(facts) - len(lines) + 1} more in the ledger)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    async def stats(self) -> dict[str, Any]:
        facts = await self._all()
        active = [fact for fact in facts if fact.status == "active"]
        return {
            "active": len(active),
            "retired": len(facts) - len(active),
            "pinned": sum(1 for fact in active if fact.pinned),
            "limit": self.limit,
            "avg_confidence": (
                round(sum(fact.confidence for fact in active) / len(active), 3) if active else 0.0
            ),
            "total_reinforcements": sum(fact.reinforcements for fact in active),
        }


def build_canonical_ledger(store: Any, settings: Any = None) -> CanonicalLedger:
    return CanonicalLedger(
        store,
        limit=int(getattr(settings, "canonical_facts_limit", 40) or 40),
        promote_after=int(getattr(settings, "canonical_promote_after", 3) or 3),
    )
