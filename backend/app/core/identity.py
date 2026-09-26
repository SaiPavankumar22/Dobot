"""Identity: who Dobot is working for, the rules it may never break, and where you are going.

Three markdown files in ``DOBOT_STATE_DIR/identity``, each with a different owner:

* ``GENOME.md`` - **yours**. Safety axioms and standing constraints, written in prose. Dobot never
  edits this file: not the model, not the consolidation pass, not an upgrade. The fenced
  ```` ```dobot-rules ```` block inside it is additionally compiled into hard runtime rules by
  :mod:`app.security.interceptors`, so an axiom is not merely advice to the model.
* ``TELOS.md`` - **yours**, with a documented API. Current state, ideal state, constraints. This is the
  answer to "what is all this for?" - without it an assistant is just autocomplete with a filesystem.
* ``MEMORY.md`` - **Dobot's**. Its evolving working notes: what it has learned about how you work.
  Rewritten by the consolidation pass, never by the model mid-task.

Composition matters here: identity is injected on **every** turn with zero model calls, because the
facts that make an assistant feel personal should not have to win a similarity search to be present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)

GENOME = "GENOME.md"
TELOS = "TELOS.md"
MEMORY = "MEMORY.md"

MAX_DOCUMENT_CHARS = 64_000
#: Only the user may author axioms and goals; the system may only write its own working memory.
USER_OWNED = (GENOME, TELOS)

GENOME_TEMPLATE = """# GENOME.md

Immutable operating rules for this assistant. Dobot reads this file on every task and **never rewrites
it**. Edit it by hand whenever you like - the file is plain markdown and lives in your own state
directory.

## Identity

- Assistant name: {assistant}
- Principal (who this assistant works for): {principal}

## Axioms

Standing rules, in plain language. Keep them few and absolute.

- Nothing leaves this machine unless I ask for it.
- Never send, post, or purchase anything on my behalf without asking first.
- Treat credential stores (~/.ssh, ~/.aws, browser profiles) as unreadable, always.
- If you are unsure whether something is reversible, ask.

## Enforceable rules

The block below is not prose: it is compiled into hard runtime rules and evaluated before every tool
call, so a matching action is stopped even if the model decided otherwise. Add your own.

- `action`: BLOCK refuses the call outright. APPROVAL stops for your confirmation. ANNOTATE only
  attaches a note.
- `match.tool`: which tools the rule applies to (omit for all of them).
- `when`: `path_contains`, `arg_contains`, `command_matches` (regex), `param_matches`, or `always`.

```dobot-rules
[
  {{
    "id": "no-credential-access",
    "action": "BLOCK",
    "reason": "Credential stores are never touched, per GENOME.md",
    "match": {{ "tool": ["fs_read", "fs_write", "fs_copy", "fs_move", "terminal"] }},
    "when": {{ "path_contains": ["~/.ssh", "~/.aws", "~/.gnupg", "~/.kube", "id_rsa"] }}
  }},
  {{
    "id": "ask-before-mass-write",
    "action": "APPROVAL",
    "reason": "A skill that changes many files at once should always be confirmed",
    "match": {{ "tool": ["skill_run"] }},
    "when": {{ "always": true }}
  }}
]
```

## Notes

Anything else you want Dobot to know about how you want it to behave. This section is context, not
enforcement.
"""

TELOS_TEMPLATE = """# TELOS.md

Where you are, where you are going, and what is in the way. Dobot reads this before planning, so a
request is judged against your goal and not only against the words in it. Be concrete; vague goals
produce vague help.

## Current State

What is true today. Projects in flight, what your week looks like, what is currently on fire.

- (fill this in - e.g. "Building X. Two client projects. Learning Y.")

## Ideal State

What "done" looks like, in a form you could recognise if you saw it.

- (fill this in - e.g. "X shipped and earning. Mornings free for deep work.")

## Constraints

Limits that must be respected: time, money, tools you do not have, things you will not do.

- (fill this in - e.g. "No more than 10 hours a week on this. Nothing that touches client data.")

## Next

The immediate moves. Keep this list short; it is what Dobot will offer to help with first.

- (fill this in)
"""

MEMORY_TEMPLATE = """# MEMORY.md

Dobot's own working notes: what it has learned about how you work. This file is maintained
automatically by the consolidation pass and is safe to read at any time. Editing it by hand is allowed
but the next consolidation may reorganise it.

## Working notes

- (nothing learned yet)
"""


@dataclass(frozen=True)
class IdentitySnapshot:
    """A read of the three documents, cached until one of them changes on disk."""

    genome: str = ""
    telos: str = ""
    memory: str = ""
    axioms: tuple[str, ...] = ()
    telos_fields: dict[str, str] = field(default_factory=dict)
    revision: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "genome_chars": len(self.genome),
            "telos_chars": len(self.telos),
            "memory_chars": len(self.memory),
            "axioms": list(self.axioms),
            "telos_fields": self.telos_fields or {},
            "revision": self.revision,
        }


def _bullets_under(text: str, heading: str) -> list[str]:
    """Collect the ``-`` bullets in the section whose heading contains ``heading`` (case-insensitive)."""
    collected: list[str] = []
    active = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            active = heading.lower() in line.lower()
            continue
        if not active:
            continue
        stripped = line.strip()
        if stripped.startswith(("-", "*")) and len(stripped) > 2:
            collected.append(stripped.lstrip("-* ").strip())
    return collected


def _sections(text: str) -> dict[str, str]:
    """Split a markdown document into ``{heading: body}`` at level-2 headings."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if line.startswith("# ") and current is None:
            continue
        if current is not None:
            sections[current].append(line)
    return {heading: "\n".join(body).strip() for heading, body in sections.items()}


class IdentityLayer:
    """Reads and writes the three identity documents. Never raises on a missing or broken file."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._dir: Path = Path(settings.state_dir) / "identity"
        self._cache: IdentitySnapshot | None = None
        self._stamp: tuple[float, float, float] | None = None

    # ------------------------------------------------------------------ paths

    @property
    def directory(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def path(self, name: str) -> Path:
        safe = os.path.basename(name)
        if safe not in {GENOME, TELOS, MEMORY}:
            raise ValueError(f"unknown identity document: {name!r}")
        return self.directory / safe

    # ------------------------------------------------------------------ templates

    def _templates(self) -> dict[str, str]:
        principal = (getattr(self.settings, "principal_name", "") or "").strip() or "the user"
        assistant = (getattr(self.settings, "assistant_name", "") or "Dobot").strip()
        return {
            GENOME: GENOME_TEMPLATE.format(assistant=assistant, principal=principal),
            TELOS: TELOS_TEMPLATE,
            MEMORY: MEMORY_TEMPLATE,
        }

    async def ensure(self) -> dict[str, Any]:
        """Create any missing document from its template. Never overwrites one that exists."""
        created: list[str] = []
        for name, template in self._templates().items():
            target = self.path(name)
            if target.exists():
                continue
            try:
                target.write_text(template, encoding="utf-8")
                created.append(name)
            except OSError as exc:  # pragma: no cover - disk failure
                logger.warning("could not seed %s (%s)", name, exc)
        if created:
            self._cache = None
            logger.info("seeded identity documents: %s", ", ".join(created))
        return {"directory": str(self.directory), "created": created}

    # ------------------------------------------------------------------ read

    def _stamp_now(self) -> tuple[float, float, float]:
        stamps: list[float] = []
        for name in (GENOME, TELOS, MEMORY):
            try:
                stamps.append(self.path(name).stat().st_mtime)
            except OSError:
                stamps.append(0.0)
        return tuple(stamps)  # type: ignore[return-value]

    async def snapshot(self, *, refresh: bool = False) -> IdentitySnapshot:
        stamp = self._stamp_now()
        if self._cache is not None and not refresh and stamp == self._stamp:
            return self._cache
        genome = await self.read(GENOME)
        telos = await self.read(TELOS)
        memory = await self.read(MEMORY)
        snapshot = IdentitySnapshot(
            genome=genome,
            telos=telos,
            memory=memory,
            axioms=tuple(_bullets_under(genome, "axioms")),
            telos_fields=_sections(telos),
            revision=f"{stamp[0]:.0f}-{stamp[1]:.0f}-{stamp[2]:.0f}",
        )
        self._cache = snapshot
        self._stamp = stamp
        return snapshot

    async def read(self, name: str) -> str:
        try:
            path = self.path(name)
        except ValueError:
            return ""
        try:
            if not path.exists():
                return ""
            return path.read_text(encoding="utf-8")[:MAX_DOCUMENT_CHARS]
        except OSError as exc:
            logger.warning("could not read %s (%s)", name, exc)
            return ""

    # ------------------------------------------------------------------ write

    async def write(self, name: str, text: str, *, actor: str = "user") -> dict[str, Any]:
        """Write a document, enforcing two invariants.

        One: the system (the model, consolidation, an upgrade) may **never** write ``GENOME.md`` or
        ``TELOS.md``. Only you can redefine your own rules and goals. Two: nothing is ever truncated to
        empty - an accidental empty write is refused rather than silently wiping your axioms.
        """
        path = self.path(name)
        if actor != "user" and name in USER_OWNED:
            raise PermissionError(f"{name} can only be written by the user, not by {actor}")
        if not text.strip():
            raise ValueError(f"refusing to overwrite {name} with empty content")
        if len(text) > MAX_DOCUMENT_CHARS:
            raise ValueError(f"{name} exceeds the {MAX_DOCUMENT_CHARS} character limit")
        path.write_text(text, encoding="utf-8")
        self._cache = None
        self._stamp = None
        return {"name": name, "chars": len(text), "path": str(path)}

    async def update_working_memory(self, text: str) -> dict[str, Any]:
        """Replace the automatically maintained working notes (system-owned)."""
        return await self.write(MEMORY, text, actor="system")

    # ------------------------------------------------------------------ views

    async def axioms(self) -> list[str]:
        return list((await self.snapshot()).axioms)

    async def genome_markdown(self) -> str:
        return (await self.snapshot()).genome

    async def render_for_prompt(self, *, max_chars: int = 2400) -> str:
        """The compact block injected into every planning prompt.

        Genome goes first and is never dropped, because a hard rule that got trimmed for length is not
        a hard rule. Goals and working notes are trimmed from the end.
        """
        snapshot = await self.snapshot()
        lines: list[str] = []
        axiom_lines = list(snapshot.axioms)[:12]
        if axiom_lines:
            lines.append("[STANDING RULES - from your GENOME.md, also enforced in code]")
            lines.extend(f"- {axiom}" for axiom in axiom_lines)
        fields = snapshot.telos_fields or {}
        current = fields.get("Current State") or ""
        ideal = fields.get("Ideal State") or ""
        constraints = fields.get("Constraints") or ""
        goals: list[str] = []
        if current:
            goals.append(f"Current state: {' '.join(current.split())[:400]}")
        if ideal:
            goals.append(f"Ideal state: {' '.join(ideal.split())[:400]}")
        if constraints:
            goals.append(f"Constraints: {' '.join(constraints.split())[:300]}")
        if goals:
            lines.append("[TELOS - where the user is trying to get to]")
            lines.extend(goals)
        notes = _bullets_under(snapshot.memory, "working notes")[:8]
        if notes:
            lines.append("[WORKING NOTES - what Dobot has learned about this user]")
            lines.extend(f"- {note}" for note in notes)
        if not lines:
            return ""
        return "\n".join(lines)[:max_chars]

    async def summarise(self) -> dict[str, Any]:
        snapshot = await self.snapshot()
        return {
            "directory": str(self.directory),
            "documents": {
                GENOME: {"chars": len(snapshot.genome), "owned_by": "user"},
                TELOS: {"chars": len(snapshot.telos), "owned_by": "user"},
                MEMORY: {"chars": len(snapshot.memory), "owned_by": "system"},
            },
            "axioms": list(snapshot.axioms),
            "telos_fields": sorted((snapshot.telos_fields or {}).keys()),
            "revision": snapshot.revision,
        }


def build_identity(settings: Any) -> IdentityLayer:
    return IdentityLayer(settings)
