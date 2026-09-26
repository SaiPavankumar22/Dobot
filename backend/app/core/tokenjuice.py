"""TokenJuice — compress tool and context output before it reaches the model.

A large part of an agent's spend is not the user's question, it is the *return value*: a directory
listing with four hundred entries, a screen dump, a research blob, a log. Most of that is redundant
(the same path repeated, the same header line, a trailing blank run) and redundant tokens are the most
expensive kind — they buy nothing.

This module shrinks those blobs with four reversible-by-design strategies, and — the important part —
**records what it saved**, so the claim is measurable rather than marketing:

* ``dedupe``      — collapse runs of identical lines into one line plus a count.
* ``json``        — walk the structure, keep the first N items of a long list and the head of a long
                    string, and replace the rest with an explicit count so nothing is silently lost.
* ``head-tail``   — keep the beginning and the end (a log's error is usually at the end) with a marker.
* ``passthrough`` — blobs below ``min_chars`` are untouched, because compressing them would cost more
                    than it saves.

Nothing here is lossy in a way the model can be misled by: every elision is annotated with the number
of items or characters removed, and the **full** output is still kept on the step record and shown in
the UI. Only the copy sent to the reasoner is compressed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import mean
from typing import Any

#: Rough English/code ratio. Used only for the *reported* token estimate, never for a hard limit.
CHARS_PER_TOKEN = 4

_ELISION = "\n…[{removed} {unit} elided by tokenjuice]…\n"
_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def estimate_tokens(chars: int) -> int:
    """A stable, cheap token estimate (≈4 characters per token)."""
    return max(0, chars) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class Compression:
    """The result of one compression, with the evidence for its claim."""

    label: str
    text: str
    original_chars: int
    sent_chars: int
    strategy: str = "passthrough"
    task_id: str = ""
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.sent_chars)

    @property
    def saved_tokens(self) -> int:
        return estimate_tokens(self.saved_chars)

    @property
    def saved_pct(self) -> float:
        if self.original_chars <= 0:
            return 0.0
        return round(self.saved_chars / self.original_chars * 100, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "strategy": self.strategy,
            "original_chars": self.original_chars,
            "sent_chars": self.sent_chars,
            "saved_chars": self.saved_chars,
            "saved_tokens": self.saved_tokens,
            "saved_pct": self.saved_pct,
            "task_id": self.task_id,
            "at": self.at.isoformat(),
        }


class SavingsLedger:
    """Accumulates every compression so the saving can be audited, not asserted.

    Bounded on purpose: the newest :attr:`limit` compressions are kept, and the running totals cover
    everything ever seen. An unbounded ledger in a long-lived always-on process is a leak.
    """

    def __init__(self, *, limit: int = 200) -> None:
        self.limit = limit
        self._recent: list[Compression] = []
        self._calls = 0
        self._original = 0
        self._sent = 0
        self._by_label: dict[str, dict[str, int]] = {}
        self._by_strategy: dict[str, int] = {}
        self._compressed_calls = 0

    def record(self, compression: Compression) -> Compression:
        self._calls += 1
        self._original += compression.original_chars
        self._sent += compression.sent_chars
        if compression.saved_chars > 0:
            self._compressed_calls += 1
        bucket = self._by_label.setdefault(
            compression.label, {"calls": 0, "original_chars": 0, "sent_chars": 0}
        )
        bucket["calls"] += 1
        bucket["original_chars"] += compression.original_chars
        bucket["sent_chars"] += compression.sent_chars
        self._by_strategy[compression.strategy] = self._by_strategy.get(compression.strategy, 0) + 1
        self._recent.append(compression)
        if len(self._recent) > self.limit:
            del self._recent[: len(self._recent) - self.limit]
        return compression

    # ------------------------------------------------------------------ views

    @property
    def saved_chars(self) -> int:
        return max(0, self._original - self._sent)

    def summary(self) -> dict[str, Any]:
        pct = round(self.saved_chars / self._original * 100, 1) if self._original else 0.0
        return {
            "calls": self._calls,
            "compressed_calls": self._compressed_calls,
            "original_chars": self._original,
            "sent_chars": self._sent,
            "saved_chars": self.saved_chars,
            "saved_tokens": estimate_tokens(self.saved_chars),
            "saved_pct": pct,
            "by_label": {
                label: {
                    **totals,
                    "saved_chars": max(0, totals["original_chars"] - totals["sent_chars"]),
                }
                for label, totals in sorted(self._by_label.items())
            },
            "by_strategy": dict(sorted(self._by_strategy.items())),
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self._recent[-limit:]][::-1]

    def reset(self) -> None:
        """Clear the ledger. Totals are session-scoped, so this is safe and idempotent."""
        self._recent.clear()
        self._by_label.clear()
        self._by_strategy.clear()
        self._calls = 0
        self._original = 0
        self._sent = 0
        self._compressed_calls = 0


# ---------------------------------------------------------------------- shrinking


def _dedupe_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Collapse *consecutive* identical lines into one line plus a count.

    Consecutive-only is deliberate: ``a b a`` means something order-dependent, and silently merging
    the two ``a`` lines would change the meaning of the output.
    """
    out: list[str] = []
    changed = False
    index = 0
    while index < len(lines):
        line = lines[index]
        run = 1
        while index + run < len(lines) and lines[index + run] == line:
            run += 1
        if run > 1:
            out.append(f"{line}   (×{run})")
            changed = True
        else:
            out.append(line)
        index += run
    return out, changed


def _head_tail(lines: list[str], head: int, tail: int) -> str:
    if len(lines) <= head + tail:
        return "\n".join(lines)
    removed = len(lines) - head - tail
    marker = _ELISION.format(removed=removed, unit="lines")
    return "\n".join(lines[:head]) + marker + "\n".join(lines[-tail:])


def _shrink_string(value: str, *, per_string: int = 240) -> Any:
    if len(value) <= per_string:
        return value
    head = value[:per_string].rstrip()
    return f"{head} …(+{len(value) - len(head)} chars)"


def _shrink_json(value: Any, *, list_keep: int, per_string: int, depth: int = 0) -> Any:
    """Recursively keep the shape of a structure while bounding its size.

    Long lists keep their first ``list_keep`` items and gain an explicit ``__elided__`` count, so the
    model can tell the difference between "there were four entries" and "there were four hundred".
    """
    if depth > 6:
        return _shrink_string(str(value), per_string=per_string)
    if isinstance(value, dict):
        return {
            str(key): _shrink_json(item, list_keep=list_keep, per_string=per_string, depth=depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, (list, tuple)):
        kept = [
            _shrink_json(item, list_keep=list_keep, per_string=per_string, depth=depth + 1)
            for item in value[:list_keep]
        ]
        if len(value) > list_keep:
            kept.append({"__elided__": len(value) - list_keep})
        return kept
    if isinstance(value, str):
        return _shrink_string(value, per_string=per_string)
    return value


class TokenJuice:
    """Compresses text and structured output, and measures what it saved."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_chars: int = 1200,
        max_chars: int = 4000,
        head_lines: int = 40,
        tail_lines: int = 25,
        ledger: SavingsLedger | None = None,
    ) -> None:
        self.enabled = enabled
        self.min_chars = max(0, min_chars)
        self.max_chars = max(200, max_chars)
        self.head_lines = max(1, head_lines)
        self.tail_lines = max(1, tail_lines)
        self.ledger = ledger or SavingsLedger()

    # ------------------------------------------------------------------ entry points

    def compress(
        self,
        label: str,
        value: Any,
        *,
        max_chars: int | None = None,
        task_id: str = "",
    ) -> Compression:
        """Compress ``value`` for the model, recording the result on the ledger.

        ``value`` may be a string, or any JSON-serialisable structure. A structure is rendered
        compactly first, so the model sees data rather than pretty-printed indentation.
        """
        budget = max_chars or self.max_chars
        if isinstance(value, str):
            original = value
            if self.enabled:
                text, strategy = self._compress_text(original, budget)
            else:
                text, strategy = original, "disabled"
        else:
            try:
                original = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
            except (TypeError, ValueError):
                original = str(value)
            if self.enabled:
                text, strategy = self._compress_json(original, budget)
            else:
                text, strategy = original, "disabled"
        return self.ledger.record(
            Compression(
                label=label,
                text=text,
                original_chars=len(original),
                sent_chars=len(text),
                strategy=strategy,
                task_id=task_id,
            )
        )

    def compress_many(self, label: str, parts: Iterable[str], *, delimiter: str = "\n") -> Compression:
        """Compress a joined set of parts as one blob (one ledger entry, one strategy decision)."""
        return self.compress(label, delimiter.join(str(part) for part in parts))

    # ------------------------------------------------------------------ strategies

    def _compress_text(self, text: str, budget: int) -> tuple[str, str]:
        if not self.enabled or len(text) < self.min_chars:
            return text, "passthrough"

        # A blob that is already JSON (a lot of tool output is) gets the structural treatment first.
        stripped = text.strip()
        if stripped[:1] in {"{", "["} and stripped[-1:] in {"}", "]"}:
            structured, strategy = self._compress_json(text, budget)
            if strategy == "json":
                return structured, strategy

        normalised = _TRAILING_WS.sub("", text)
        normalised = _BLANK_RUN.sub("\n\n", normalised)
        lines = normalised.splitlines()
        deduped, changed = _dedupe_lines(lines)
        working = deduped if changed else lines

        if len("\n".join(working)) <= budget:
            return "\n".join(working), "dedupe" if changed else "passthrough"

        shrunk = _head_tail(working, self.head_lines, self.tail_lines)
        if len(shrunk) <= budget:
            return shrunk, "head-tail"

        # Still over budget: widen the cut proportionally, then hard-cap as the last resort.
        keep = max(1, budget // 2)
        head = "\n".join(working[: self.head_lines])
        tail_budget = max(0, keep)
        tail = "\n".join(working[-self.tail_lines :])[-tail_budget:]
        removed_part = max(0, len(working) - self.head_lines - self.tail_lines)
        combined = head + _ELISION.format(removed=removed_part, unit="lines") + tail
        return combined[:budget], "head-tail+cap"

    def _compress_json(self, text: str, budget: int) -> tuple[str, str]:
        if not self.enabled or len(text) < self.min_chars:
            return text, "passthrough"
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text, "passthrough"

        list_keep = max(3, min(40, budget // 80))
        shrunk = _shrink_json(parsed, list_keep=list_keep, per_string=240)
        rendered = json.dumps(shrunk, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(rendered) <= budget:
            return rendered, "json"

        # Even the shrunk structure is too big: cut the item budget, then fall back to text rules.
        tighter = _shrink_json(parsed, list_keep=3, per_string=120)
        rendered = json.dumps(tighter, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(rendered) <= budget:
            return rendered, "json"
        return self._compress_text(rendered, budget)

    # ------------------------------------------------------------------ reporting

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            **self.ledger.summary(),
        }


def build_tokenjuice(settings: Any) -> TokenJuice:
    """Build from :class:`app.config.Settings`."""
    return TokenJuice(
        enabled=bool(getattr(settings, "tokenjuice_enabled", True)),
        min_chars=int(getattr(settings, "tokenjuice_min_chars", 1200)),
        max_chars=int(getattr(settings, "tokenjuice_max_chars", 4000)),
        head_lines=int(getattr(settings, "tokenjuice_head_lines", 40)),
        tail_lines=int(getattr(settings, "tokenjuice_tail_lines", 25)),
    )


def average_saved_pct(compressions: Iterable[Compression]) -> float:
    """Mean saving across compressions that actually did something (0.0 when none did)."""
    active = [item.saved_pct for item in compressions if item.saved_chars > 0]
    return round(mean(active), 1) if active else 0.0
