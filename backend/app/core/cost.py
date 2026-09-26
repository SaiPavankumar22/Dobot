"""Cost accounting and the run journal — what a task spent, and what it did, in order.

Two related problems, one module.

**Spend.** An always-on agent that runs unattended has to be able to answer "what did that cost me?"
Every model call is recorded with its token counts and an *estimated* cost. If no price is configured
the tokens are still counted and the money is reported as unknown rather than guessed — a ledger that
invents a price is worse than no ledger.

**Replay.** A task is a sequence of decisions, tool calls, compressions, and model calls. Keeping that
sequence durably (not in memory) means a failure can be reconstructed after the fact, and a resumed
task can be picked up without re-deriving what already happened.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)

RUNS_COLLECTION = "runs"


@dataclass(frozen=True)
class UsageEntry:
    """One model call."""

    model: str = ""
    tier: str = ""
    purpose: str = "chat"
    task_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    priced: bool = False
    degraded: bool = False
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tier": self.tier,
            "purpose": self.purpose,
            "task_id": self.task_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 6),
            "priced": self.priced,
            "degraded": self.degraded,
            "at": self.at.isoformat(),
        }


def _tokens_from_usage(usage: dict[str, Any]) -> tuple[int, int, int]:
    """Read OpenAI-style usage tolerantly; providers vary and some omit it entirely."""
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (prompt + completion)
    return prompt, completion, total


class CostLedger:
    """Session-scoped token and cost accounting, bounded so it cannot grow without limit."""

    def __init__(self, *, limit: int = 500) -> None:
        self.limit = limit
        self._entries: list[UsageEntry] = []
        self._by_task: dict[str, dict[str, float]] = {}

    def record(self, entry: UsageEntry) -> UsageEntry:
        self._entries.append(entry)
        if len(self._entries) > self.limit:
            del self._entries[: len(self._entries) - self.limit]
        if entry.task_id:
            bucket = self._by_task.setdefault(
                entry.task_id,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += entry.prompt_tokens
            bucket["completion_tokens"] += entry.completion_tokens
            bucket["cost_usd"] = round(bucket["cost_usd"] + entry.cost_usd, 6)
            if len(self._by_task) > 2000:  # pragma: no cover - long-lived process guard
                self._by_task.pop(next(iter(self._by_task)), None)
        return entry

    def summary(self) -> dict[str, Any]:
        prompt = sum(entry.prompt_tokens for entry in self._entries)
        completion = sum(entry.completion_tokens for entry in self._entries)
        cost = sum(entry.cost_usd for entry in self._entries)
        by_model: dict[str, dict[str, Any]] = {}
        by_tier: dict[str, int] = {}
        by_purpose: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            key = entry.model or "unknown"
            bucket = by_model.setdefault(
                key,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += entry.prompt_tokens
            bucket["completion_tokens"] += entry.completion_tokens
            bucket["cost_usd"] = round(bucket["cost_usd"] + entry.cost_usd, 6)
            by_tier[entry.tier or "unknown"] = by_tier.get(entry.tier or "unknown", 0) + 1
            purpose = by_purpose.setdefault(entry.purpose, {"calls": 0, "total_tokens": 0})
            purpose["calls"] += 1
            purpose["total_tokens"] += entry.total_tokens
        latencies = [entry.latency_ms for entry in self._entries if entry.latency_ms]
        return {
            "calls": len(self._entries),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "estimated_cost_usd": round(cost, 6),
            "priced": any(entry.priced for entry in self._entries),
            "degraded_calls": sum(1 for entry in self._entries if entry.degraded),
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
            "by_model": dict(sorted(by_model.items())),
            "by_tier": dict(sorted(by_tier.items())),
            "by_purpose": dict(sorted(by_purpose.items())),
        }

    def for_task(self, task_id: str) -> dict[str, Any]:
        bucket = self._by_task.get(task_id)
        if not bucket:
            return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
        return {"task_id": task_id, **bucket}

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        return [entry.as_dict() for entry in self._entries[-limit:]][::-1]

    def reset(self) -> None:
        self._entries.clear()
        self._by_task.clear()


_LEDGER = CostLedger()


def get_cost_ledger() -> CostLedger:
    return _LEDGER


# ---------------------------------------------------------------------- scoping

_scope: ContextVar[dict[str, str] | None] = ContextVar("dobot_cost_scope", default=None)


@contextmanager
def cost_scope(*, task_id: str = "", purpose: str = "") -> Iterator[None]:
    """Label model calls made inside this block with a task and a purpose.

    A ContextVar rather than an attribute, because tasks run concurrently: two requests must not be
    able to attribute each other's spend. ContextVars are copied into child asyncio tasks, so a
    background task keeps its own label.
    """
    current = dict(_scope.get() or {})
    if task_id:
        current["task_id"] = task_id
    if purpose:
        current["purpose"] = purpose
    token = _scope.set(current)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> dict[str, str]:
    return dict(_scope.get() or {})


def record_usage(
    *,
    model: str,
    tier: str,
    usage: dict[str, Any],
    latency_ms: int = 0,
    degraded: bool = False,
    settings: Any = None,
    ledger: CostLedger | None = None,
) -> UsageEntry:
    """Record one model call, estimating cost only when a price is configured."""
    prompt, completion, total = _tokens_from_usage(usage or {})
    priced = False
    cost = 0.0
    if settings is not None and getattr(settings, "pricing_configured", False):
        cost = settings.estimate_cost_usd(input_tokens=prompt, output_tokens=completion)
        priced = True
    scope = current_scope()
    entry = UsageEntry(
        model=model,
        tier=tier,
        purpose=scope.get("purpose", "chat"),
        task_id=scope.get("task_id", ""),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        latency_ms=latency_ms,
        cost_usd=cost,
        priced=priced,
        degraded=degraded,
    )
    return (ledger or get_cost_ledger()).record(entry)


# ---------------------------------------------------------------------- journal


class RunJournal:
    """A durable, ordered record of what a task did.

    Stored in the record store, one document per task, so it survives a restart — which is what makes
    a paused task genuinely resumable rather than merely re-runnable.
    """

    def __init__(self, store: Any, *, limit: int = 400) -> None:
        self.store = store
        self.limit = max(20, limit)

    async def append(self, task_id: str, kind: str, detail: str, **meta: Any) -> dict[str, Any]:
        """Append one entry. Never raises: a journal must not be able to fail a task."""
        if not task_id:
            return {}
        entry = {
            "kind": kind,
            "detail": detail[:600],
            "at": datetime.now(UTC).isoformat(),
            "meta": {key: value for key, value in meta.items() if value is not None},
        }
        try:
            doc = await self.store.get(RUNS_COLLECTION, task_id)
            entries: list[dict[str, Any]] = list((doc or {}).get("entries") or [])
            entry["seq"] = len(entries) + 1
            entries.append(entry)
            if len(entries) > self.limit:
                entries = entries[-self.limit :]
            await self.store.insert(
                RUNS_COLLECTION,
                {"id": task_id, "task_id": task_id, "entries": entries, "updated_at": entry["at"]},
            )
        except Exception as exc:  # noqa: BLE001 - the journal is observability, never a dependency
            logger.info("run journal append failed (%s)", exc)
            return {}
        return entry

    async def entries(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        doc = await self.store.get(RUNS_COLLECTION, task_id)
        return list((doc or {}).get("entries") or [])[-limit:]

    async def clear(self, task_id: str) -> bool:
        return await self.store.delete(RUNS_COLLECTION, task_id)

    async def count(self) -> int:
        return await self.store.count(RUNS_COLLECTION)

    async def prune(self, *, keep: int = 200, protect: set[str] | None = None) -> int:
        """Drop the oldest journals once there are more than ``keep`` of them.

        The local store rewrites a whole collection on every write, so an unbounded journal
        collection would get slower with every task ever run. Bounded is the only honest design.
        """
        keep = max(10, keep)
        protected = protect or set()
        try:
            docs = await self.store.all(RUNS_COLLECTION, limit=10_000)
        except Exception:  # noqa: BLE001
            return 0
        ordered = sorted(docs, key=lambda doc: str(doc.get("updated_at") or ""))
        doomed = [
            str(doc.get("id"))
            for doc in ordered[: max(0, len(ordered) - keep)]
            if doc.get("id") and str(doc.get("id")) not in protected
        ]
        removed = 0
        for task_id in doomed:
            if await self.clear(task_id):
                removed += 1
        return removed
