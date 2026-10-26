"""Consolidation - the pass that keeps memory useful instead of merely large.

An always-on assistant accumulates memories faster than anyone prunes them, and the cost of that is
paid entirely at retrieval time: the more you store, the less reliably the right thing comes back. So
this module runs on a timer (and on demand) to do five deterministic things:

1. **Decay.** Every memory has a retention score, an Ebbinghaus-style curve over how long it has been
   since it was last useful, weighted by how important it was and how often it has been recalled. Use
   reinforces; neglect fades.
2. **Forget.** Memories whose retention has fallen below the threshold are dropped. Nothing important
   is ever silently dropped: high-importance memories and pinned facts are exempt.
3. **Merge.** Duplicates of the same statement collapse to one, which is the cheapest large win - a
   hundred "user is building Dobot" entries cost a hundred times an entry's retrieval budget and
   return one fact.
4. **Promote.** Statements seen repeatedly graduate into the canonical ledger, where they stop having
   to be retrieved at all.
5. **Write the working notes.** A short, human-readable ``MEMORY.md`` describing what has actually been
   learned, so the system's self-model is inspectable rather than implied.

Pass A (deterministic) does all of the above with no model calls — it can run unattended for free.
Pass B (review, optional) uses a cheap model to propose edits: merge near-duplicates that differ by
wording, flag stale facts, and suggest canonical promotions. Proposals are stored for Doctor review
and auto-applied only when ``consolidation_auto_apply_reviews`` is True and importance < 0.5.
Secrets are redacted before any prompt is constructed.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.events import EventBus, EventType, get_event_bus
from app.logging_setup import get_logger

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|bearer|access[_-]key|private[_-]key)\s*[=:]\s*\S+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Strip credential-like patterns before any text reaches a model prompt."""
    return _SECRET_RE.sub(lambda m: m.group(0).split("=")[0] + "=<redacted>", text)

logger = get_logger(__name__)

#: Memories at or above this importance are never forgotten by decay, only by an explicit request.
PROTECTED_IMPORTANCE = 0.75
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
TAG_STOPWORDS = {"episode", "task", "chat", "orchestrator", "manual", "automation"}


def _normalise(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").lower())).strip()


def retention(
    *,
    importance: float,
    access_count: int,
    age_days: float,
    half_life_days: float = 21.0,
) -> float:
    """Ebbinghaus retention in ``[0, 1]``.

    ``R = exp(-age / strength)`` where strength grows with importance and with how often the memory has
    actually been recalled. A memory recalled ten times decays ten times more slowly than one that has
    never been used, which is the behaviour that matters: use is the signal, not age.
    """
    importance = max(0.0, min(1.0, importance))
    strength = half_life_days * (0.35 + importance) * (1.0 + math.log1p(max(0, access_count)))
    if strength <= 0:
        return 0.0
    return round(math.exp(-max(0.0, age_days) / strength), 4)


@dataclass
class ConsolidationReport:
    scanned: int = 0
    forgotten: int = 0
    merged: int = 0
    promoted: int = 0
    reinforced: int = 0
    canonical_retired: int = 0
    notes_written: bool = False
    dry_run: bool = False
    duration_ms: int = 0
    protected: int = 0
    journals_pruned: int = 0
    weakest: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "forgotten": self.forgotten,
            "merged": self.merged,
            "promoted": self.promoted,
            "reinforced": self.reinforced,
            "canonical_retired": self.canonical_retired,
            "notes_written": self.notes_written,
            "dry_run": self.dry_run,
            "duration_ms": self.duration_ms,
            "protected": self.protected,
            "journals_pruned": self.journals_pruned,
            "weakest": self.weakest,
        }

    def summary_line(self) -> str:
        return (
            f"scanned {self.scanned}, merged {self.merged}, forgot {self.forgotten}, "
            f"promoted {self.promoted} in {self.duration_ms} ms"
        )


@dataclass
class ReviewProposal:
    """One proposed edit from pass B that a human or auto-apply can accept/reject."""
    kind: str          # "merge" | "promote" | "flag_stale" | "note"
    memory_ids: list[str]
    content: str       # proposed new content (may be empty for flag_stale/note)
    rationale: str
    auto_applicable: bool = False  # safe to apply without human review


class Consolidator:
    """Runs the maintenance pass and, optionally, on a timer."""

    def __init__(
        self,
        *,
        memory: Any,
        canonical: Any,
        identity: Any,
        settings: Any,
        journal: Any = None,
        bus: EventBus | None = None,
        store: Any = None,
        reasoner: Any = None,
    ) -> None:
        self.memory = memory
        self.canonical = canonical
        self.identity = identity
        self.settings = settings
        # Optional, so a test can build a consolidator without a record store behind it.
        self.journal = journal
        self.bus = bus or get_event_bus()
        self._store = store           # for persisting review proposals
        self._reasoner = reasoner     # cheap model for pass B (may be None)
        self._task: asyncio.Task[None] | None = None
        self._last: ConsolidationReport | None = None
        self._last_proposals: list[ReviewProposal] = []

    # ------------------------------------------------------------------ lifecycle

    @property
    def interval_seconds(self) -> int:
        return max(300, int(getattr(self.settings, "consolidation_interval_seconds", 21_600) or 21_600))

    async def start(self) -> None:
        if not getattr(self.settings, "consolidation_enabled", True):
            logger.info("consolidation disabled by configuration")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="dobot-consolidation")
        logger.info("consolidation scheduled every %ss", self.interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with self._suppress():
            await self._task
        self._task = None

    @staticmethod
    def _suppress() -> Any:
        import contextlib

        return contextlib.suppress(asyncio.CancelledError, Exception)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.run()
            except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
                raise
            except Exception as exc:  # noqa: BLE001 - maintenance must never kill the process
                logger.warning("consolidation pass failed (%s)", exc)

    @property
    def last_report(self) -> ConsolidationReport | None:
        return self._last

    # ------------------------------------------------------------------ the pass

    async def run(self, *, dry_run: bool = False) -> ConsolidationReport:
        started = time.perf_counter()
        report = ConsolidationReport(dry_run=dry_run)
        settings = self.settings
        half_life = float(getattr(settings, "memory_half_life_days", 21.0) or 21.0)
        threshold = float(getattr(settings, "memory_forget_threshold", 0.12) or 0.12)
        decay_enabled = bool(getattr(settings, "memory_decay_enabled", True))

        try:
            records = await self.memory.list(limit=10_000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("consolidation could not list memories (%s)", exc)
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            self._last = report
            return report

        report.scanned = len(records)
        now = datetime.now(UTC)

        # --- 1 & 3: score everything, and group exact repeats so they collapse.
        grouped: dict[str, list[Any]] = {}
        retention_by_id: dict[str, float] = {}
        for record in records:
            grouped.setdefault(_normalise(str(getattr(record, "content", ""))), []).append(record)
            age_days = _age_days(now, getattr(record, "created_at", None), getattr(record, "last_access_at", None))
            score = (
                retention(
                    importance=float(getattr(record, "importance", 0.5)),
                    access_count=int(getattr(record, "access_count", 0) or 0),
                    age_days=age_days,
                    half_life_days=half_life,
                )
                if decay_enabled
                else 1.0
            )
            retention_by_id[str(getattr(record, "id", ""))] = score

        to_delete: list[str] = []
        survivors: list[Any] = []
        for _key, bucket in grouped.items():
            if len(bucket) > 1:
                bucket.sort(
                    key=lambda item: (float(getattr(item, "importance", 0.5)), -_age_days_of(now, item)),
                    reverse=True,
                )
                keeper = bucket[0]
                survivors.append(keeper)
                for duplicate in bucket[1:]:
                    to_delete.append(str(getattr(duplicate, "id", "")))
                    report.merged += 1
            else:
                survivors.append(bucket[0])

        # --- 2: forget what has faded, never what is protected.
        for record in survivors:
            importance = float(getattr(record, "importance", 0.5))
            score = retention_by_id.get(str(getattr(record, "id", "")), 1.0)
            if importance >= PROTECTED_IMPORTANCE:
                report.protected += 1
                continue
            if score >= threshold:
                continue
            to_delete.append(str(getattr(record, "id", "")))
            report.forgotten += 1
            if len(report.weakest) < 5:
                report.weakest.append(
                    {
                        "id": str(getattr(record, "id", "")),
                        "content": str(getattr(record, "content", ""))[:120],
                        "retention": score,
                        "importance": importance,
                    }
                )

        if not dry_run and to_delete:
            for memory_id in to_delete:
                try:
                    await self.memory.forget(memory_id)
                except Exception as exc:  # noqa: BLE001
                    logger.info("could not forget %s (%s)", memory_id, exc)

        # --- 4: promote repeated statements into the always-injected ledger.
        #
        # Repetition is counted from the *pre-merge* grouping on purpose. Merging runs first and
        # collapses duplicates, so by this point every key has exactly one surviving record - reading
        # the count here would always say 1 and nothing would ever promote. The merge and the
        # promotion want different things from the same data, so the count is captured before the
        # merge and passed in.
        repeats = {key: len(bucket) for key, bucket in grouped.items()}
        kept = [record for record in survivors if str(getattr(record, "id", "")) not in set(to_delete)]
        if not dry_run and self.canonical is not None:
            try:
                promoted = await self.canonical.consolidate_from(kept, repeat_counts=repeats)
                report.promoted = int(promoted.get("promoted", 0))
                report.reinforced = int(promoted.get("reinforced", 0))
            except Exception as exc:  # noqa: BLE001
                logger.warning("canonical promotion failed (%s)", exc)

        # --- 5: rewrite the working notes so the self-model stays inspectable.
        if not dry_run and self.identity is not None:
            try:
                await self.identity.update_working_memory(
                    await self._working_notes(kept, report, now=now)
                )
                report.notes_written = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not write working notes (%s)", exc)

        # --- 6: bound the run journal, which the dashboard reads and the user can replay.
        if not dry_run and self.journal is not None:
            try:
                keep = max(20, int(getattr(settings, "run_journal_keep", 200) or 200))
                report.journals_pruned = await self.journal.prune(keep=keep)
            except Exception as exc:  # noqa: BLE001
                logger.info("journal prune failed (%s)", exc)

        # --- pass B: optional LLM review -----------------------------------------
        # Only runs when a reasoner is wired and the feature is enabled in settings.
        # Secrets are redacted before any prompt is sent.
        if (
            not dry_run
            and self._reasoner is not None
            and bool(getattr(self.settings, "consolidation_review_enabled", False))
        ):
            try:
                proposals = await self._review_pass(kept)
                self._last_proposals = proposals
                await self._handle_proposals(proposals)
                if proposals:
                    logger.info("consolidation review: %d proposals", len(proposals))
            except Exception as exc:  # noqa: BLE001
                logger.warning("consolidation review pass failed (%s)", exc)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        self._last = report
        if not dry_run:
            await self.bus.emit(
                EventType.MEMORY_WRITTEN,
                message=f"Consolidation: {report.summary_line()}",
                consolidation=report.as_dict(),
            )
        logger.info("consolidation pass: %s", report.summary_line())
        return report

    # ------------------------------------------------------------------ pass B: review

    async def _review_pass(self, records: list[Any]) -> list[ReviewProposal]:
        """Ask a cheap model to propose edits across the surviving memory records.

        Only the first 40 records (by importance) are sent — the prompt cannot be unbounded.
        Secrets are stripped before the prompt is built.
        """
        if not records:
            return []
        # Sort by importance descending; cap to 40 for prompt safety.
        by_importance = sorted(
            records,
            key=lambda item: float(getattr(item, "importance", 0.5)),
            reverse=True,
        )[:40]

        lines = []
        for record in by_importance:
            rid = str(getattr(record, "id", ""))
            content = _redact(str(getattr(record, "content", ""))[:300])
            imp = float(getattr(record, "importance", 0.5))
            lines.append(f"[{rid}] (importance={imp:.2f}) {content}")

        prompt = (
            "You are reviewing a set of memory records for a local AI assistant. "
            "Identify near-duplicate memories that should be merged, stale facts that should be flagged, "
            "and frequently repeated facts that are good candidates for promotion to long-term canonical ledger. "
            "Reply with a JSON array of objects, each with keys: "
            '"kind" (merge|promote|flag_stale), "memory_ids" (array of IDs involved), '
            '"content" (proposed merged text if kind=merge, else empty), "rationale" (1 sentence). '
            "Do not invent new facts. Do not include secrets or credentials in any field. "
            "If nothing needs changing, return an empty array [].\n\n"
            "Memory records:\n" + "\n".join(lines)
        )
        try:
            import json as _json
            result = await self._reasoner.ask(prompt, max_tokens=1200)
            raw = (result or "").strip()
            # Extract the JSON array from the response (model may wrap it in prose).
            bracket = raw.find("[")
            end_bracket = raw.rfind("]")
            if bracket == -1 or end_bracket == -1:
                return []
            items = _json.loads(raw[bracket : end_bracket + 1])
            proposals: list[ReviewProposal] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind", "note"))
                memory_ids = [str(i) for i in (item.get("memory_ids") or [])]
                content = _redact(str(item.get("content") or ""))[:500]
                rationale = _redact(str(item.get("rationale") or ""))[:300]
                # Auto-apply only simple merges of low-importance records.
                auto = kind == "merge" and all(
                    float(getattr(r, "importance", 0.5)) < 0.5
                    for r in records
                    if str(getattr(r, "id", "")) in memory_ids
                )
                proposals.append(ReviewProposal(
                    kind=kind,
                    memory_ids=memory_ids,
                    content=content,
                    rationale=rationale,
                    auto_applicable=auto,
                ))
            return proposals
        except Exception as exc:  # noqa: BLE001
            logger.info("consolidation review: could not parse model response (%s)", exc)
            return []

    async def _handle_proposals(self, proposals: list[ReviewProposal]) -> None:
        """Store proposals for Doctor review; auto-apply those flagged as safe."""
        auto_apply = bool(getattr(self.settings, "consolidation_auto_apply_reviews", False))
        for proposal in proposals:
            if self._store is not None:
                doc = {
                    "id": f"prop_{int(time.time() * 1000)}_{proposal.kind}",
                    "kind": proposal.kind,
                    "memory_ids": proposal.memory_ids,
                    "content": proposal.content,
                    "rationale": proposal.rationale,
                    "auto_applicable": proposal.auto_applicable,
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                }
                try:
                    await self._store.insert("consolidation_proposals", doc)
                except Exception as exc:  # noqa: BLE001
                    logger.info("could not store consolidation proposal (%s)", exc)

            if auto_apply and proposal.auto_applicable and proposal.kind == "merge" and proposal.content:
                # Merge: replace the first memory with the new content, forget the rest.
                ids = proposal.memory_ids
                if len(ids) >= 2:
                    try:
                        await self.memory.update(ids[0], {"content": proposal.content})
                        for extra in ids[1:]:
                            await self.memory.forget(extra)
                        logger.info("auto-applied merge proposal for %s", ids[0])
                    except Exception as exc:  # noqa: BLE001
                        logger.info("auto-apply merge failed (%s)", exc)

    @property
    def last_proposals(self) -> list[ReviewProposal]:
        """Proposals from the most recent review pass — for Doctor/Settings to surface."""
        return list(self._last_proposals)

    # ------------------------------------------------------------------ notes

    async def _working_notes(
        self, records: list[Any], report: ConsolidationReport, *, now: datetime
    ) -> str:
        """A deterministic, readable summary. No model call, so it cannot hallucinate about itself."""
        by_type: dict[str, int] = {}
        tags: dict[str, int] = {}
        for record in records:
            kind = str(getattr(getattr(record, "type", ""), "value", getattr(record, "type", "")))
            by_type[kind] = by_type.get(kind, 0) + 1
            for tag in getattr(record, "tags", []) or []:
                lowered = str(tag).lower()
                if lowered not in TAG_STOPWORDS:
                    tags[lowered] = tags.get(lowered, 0) + 1

        recent = sorted(
            records,
            key=lambda item: (
                float(getattr(item, "importance", 0.5)),
                str(getattr(item, "created_at", "")),
            ),
            reverse=True,
        )[:6]

        canonical_stats: dict[str, Any] = {}
        if self.canonical is not None:
            try:
                canonical_stats = await self.canonical.stats()
            except Exception:  # noqa: BLE001
                canonical_stats = {}

        lines = [
            "# MEMORY.md",
            "",
            "Dobot's own working notes: what it has learned about how you work. Maintained automatically",
            "by the consolidation pass. Safe to read at any time.",
            "",
            "## Working notes",
            "",
            f"- Last consolidation: {now.isoformat(timespec='seconds')} "
            f"({report.summary_line()})",
            f"- Durable memories: {len(records)}"
            + (f" across {len(by_type)} kinds ({', '.join(f'{k} {v}' for k, v in sorted(by_type.items()))})" if by_type else ""),
        ]
        top_tags = [name for name, _count in sorted(tags.items(), key=lambda item: -item[1])[:6]]
        if top_tags:
            lines.append(f"- Recurring areas: {', '.join(top_tags)}")
        if canonical_stats:
            lines.append(
                f"- Canonical ledger: {canonical_stats.get('active', 0)} resident, "
                f"{canonical_stats.get('pinned', 0)} pinned, "
                f"average confidence {canonical_stats.get('avg_confidence', 0)}"
            )
        if recent:
            lines.append("")
            lines.append("## What has mattered recently")
            lines.append("")
            for record in recent:
                content = " ".join(str(getattr(record, "content", "")).split())[:180]
                if content:
                    lines.append(f"- {content}")
        lines.append("")
        return "\n".join(lines)


def _age_days(now: datetime, created: Any, last_access: Any) -> float:
    stamp = _parse(last_access) or _parse(created) or now
    return max(0.0, (now - stamp).total_seconds() / 86_400)


def _age_days_of(now: datetime, record: Any) -> float:
    return _age_days(now, getattr(record, "created_at", None), getattr(record, "last_access_at", None))


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def build_consolidator(
    *,
    memory: Any,
    canonical: Any,
    identity: Any,
    settings: Any,
    journal: Any = None,
    store: Any = None,
    reasoner: Any = None,
) -> Consolidator:
    return Consolidator(
        memory=memory,
        canonical=canonical,
        identity=identity,
        settings=settings,
        journal=journal,
        store=store,
        reasoner=reasoner,
        bus=get_event_bus(),
    )
