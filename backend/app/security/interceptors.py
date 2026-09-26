"""Pre-action interceptors — rules that fire on every tool call, by construction.

A prompt instruction is advisory: the model may follow it, and you cannot tell from the outside
whether it did. An interceptor is not advisory. It reads the live tool call *before* the tool runs and
returns a verdict the orchestrator must honour, so "never touch my tax folder" stops being a sentence
in a system prompt and becomes a code path.

Interceptors are strictly one-directional: they may make a step **stricter** (ALLOW → APPROVAL →
BLOCK) and may attach a note, but they can never loosen a decision the policy engine already made.
That keeps the safety story monotonic and easy to reason about — adding a rule can only ever remove
capability, never grant it.

Rules come from two places, both inspectable:

* ``GENOME.md`` — a fenced ```` ```dobot-rules ```` block of JSON you author yourself. These are your
  immutable axioms.
* a skill's ``workflow.json`` — an optional ``interceptors`` array, so a skill can carry its own guard
  rails (e.g. "this skill may only write inside the folder it was pointed at").
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.schemas import Verdict

logger = get_logger(__name__)

#: Markdown fence that carries machine-readable rules inside an otherwise human document.
RULES_FENCE = "dobot-rules"

_ACTIONS = {
    "BLOCK": Verdict.BLOCK,
    "BLOCKED": Verdict.BLOCK,
    "DENY": Verdict.BLOCK,
    "APPROVAL": Verdict.APPROVAL,
    "ASK": Verdict.APPROVAL,
    "CONFIRM": Verdict.APPROVAL,
    "ANNOTATE": Verdict.ALLOW,
    "NOTE": Verdict.ALLOW,
    "ALLOW": Verdict.ALLOW,
}

_STRICTNESS = {Verdict.ALLOW: 0, Verdict.APPROVAL: 1, Verdict.BLOCK: 2}

_FENCE_RE = re.compile(rf"```\s*{RULES_FENCE}\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def stricter(left: Verdict, right: Verdict) -> Verdict:
    """Return whichever verdict is more restrictive (BLOCK > APPROVAL > ALLOW)."""
    return left if _STRICTNESS.get(left, 0) >= _STRICTNESS.get(right, 0) else right


def _canonical(text: str) -> str:
    """Normalise a path-ish string so a rule matches regardless of how the path was written."""
    expanded = os.path.expanduser(text.strip())
    return str(expanded).replace("\\", "/").rstrip("/").lower()


def _iter_strings(value: Any, depth: int = 0) -> list[str]:
    """Every string inside a params dict, so a rule can match on any argument."""
    if depth > 5:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.append(str(key))
            found.extend(_iter_strings(item, depth + 1))
        return found
    if isinstance(value, (list, tuple, set)):
        found = []
        for item in value:
            found.extend(_iter_strings(item, depth + 1))
        return found
    return []


@dataclass
class InterceptorRule:
    """One deterministic rule. Everything about it is data, so it can be shown in the UI."""

    id: str
    action: str = "APPROVAL"
    reason: str = ""
    source: str = "genome"
    tools: tuple[str, ...] = ()
    always: bool = False
    path_contains: tuple[str, ...] = ()
    arg_contains: tuple[str, ...] = ()
    command_matches: tuple[str, ...] = ()
    param_matches: dict[str, str] = field(default_factory=dict)
    priority: int = 50
    _command_re: list[re.Pattern[str]] | None = None
    _param_re: dict[str, re.Pattern[str]] = field(default_factory=dict)

    @property
    def verdict(self) -> Verdict:
        return _ACTIONS.get(self.action.strip().upper(), Verdict.APPROVAL)

    def compile(self) -> list[str]:
        """Pre-compile regexes. Returns the list of problems (never raises)."""
        problems: list[str] = []
        self._command_re = []
        for pattern in self.command_matches:
            try:
                self._command_re.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                problems.append(f"{self.id}: bad command_matches regex {pattern!r} ({exc})")
        self._param_re = {}
        for name, pattern in self.param_matches.items():
            try:
                self._param_re[name] = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                problems.append(f"{self.id}: bad param_matches regex for {name!r} ({exc})")
        return problems

    def matches(self, tool: str, params: dict[str, Any]) -> bool:
        if self.tools and tool not in self.tools:
            return False
        has_condition = bool(
            self.always
            or self.path_contains
            or self.arg_contains
            or self.command_matches
            or self.param_matches
        )
        if not has_condition:
            # A tool-scoped rule with no condition is unconditional for that tool.
            return True
        if self.always:
            return True

        path_text = " ".join(_canonical(item) for item in _iter_strings(params))
        for needle in self.path_contains:
            if _canonical(needle) in path_text:
                return True
        joined = "\n".join(_iter_strings(params))
        lowered = joined.lower()
        for needle in self.arg_contains:
            if needle.lower() in lowered:
                return True
        for pattern in self._command_re or []:
            if pattern.search(joined):
                return True
        for name, pattern in (self._param_re or {}).items():
            candidate = params.get(name)
            candidates = _iter_strings(candidate) if candidate is not None else []
            if any(pattern.search(item) for item in candidates):
                return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action.strip().upper(),
            "reason": self.reason,
            "source": self.source,
            "tools": list(self.tools),
            "conditions": {
                "always": self.always,
                "path_contains": list(self.path_contains),
                "arg_contains": list(self.arg_contains),
                "command_matches": list(self.command_matches),
                "param_matches": dict(self.param_matches),
            },
            "priority": self.priority,
        }


@dataclass
class InterceptorOutcome:
    verdict: Verdict = Verdict.ALLOW
    fired: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "fired": list(self.fired),
            "reasons": list(self.reasons),
            "annotations": list(self.annotations),
        }


def _coerce_rule(raw: dict[str, Any], *, source: str, index: int) -> InterceptorRule | None:
    if not isinstance(raw, dict):
        return None
    when = raw.get("when") if isinstance(raw.get("when"), dict) else {}
    match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
    tools_raw = match.get("tool") or match.get("tools") or raw.get("tools") or []
    if isinstance(tools_raw, str):
        tools_raw = [tools_raw]
    param_matches = when.get("param_matches") or raw.get("param_matches") or {}
    if not isinstance(param_matches, dict):
        param_matches = {}
    rule_id = str(raw.get("id") or raw.get("name") or f"{source}-{index}")

    def as_tuple(key: str) -> tuple[str, ...]:
        value = when.get(key) if key in when else raw.get(key)
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value if str(item).strip())
        return ()

    return InterceptorRule(
        id=rule_id,
        action=str(raw.get("action") or raw.get("verdict") or "APPROVAL"),
        reason=str(raw.get("reason") or raw.get("message") or ""),
        source=source,
        tools=tuple(str(item) for item in tools_raw if str(item).strip()),
        always=bool(when.get("always") or raw.get("always")),
        path_contains=as_tuple("path_contains"),
        arg_contains=as_tuple("arg_contains"),
        command_matches=as_tuple("command_matches"),
        param_matches={str(key): str(value) for key, value in param_matches.items()},
        priority=int(raw.get("priority") or 50),
    )


def parse_rules(payload: Any, *, source: str) -> tuple[list[InterceptorRule], list[str]]:
    """Parse one already-decoded JSON payload into rules, collecting problems instead of raising."""
    if isinstance(payload, dict):
        payload = payload.get("interceptors") or payload.get("rules") or []
    if not isinstance(payload, list):
        return [], [f"{source}: expected a list of rules"]
    rules: list[InterceptorRule] = []
    problems: list[str] = []
    for index, raw in enumerate(payload):
        rule = _coerce_rule(raw, source=source, index=index)
        if rule is None:
            problems.append(f"{source}: rule {index} is not an object")
            continue
        problems.extend(f"{source}: {problem}" for problem in rule.compile())
        rules.append(rule)
    return rules, problems


def extract_rules_from_markdown(text: str, *, source: str) -> tuple[list[InterceptorRule], list[str]]:
    """Pull every fenced ```dobot-rules JSON block out of a markdown document."""
    rules: list[InterceptorRule] = []
    problems: list[str] = []
    blocks = _FENCE_RE.findall(text or "")
    if not blocks:
        return [], []
    for index, block in enumerate(blocks):
        try:
            payload = json.loads(block)
        except (json.JSONDecodeError, ValueError) as exc:
            problems.append(f"{source}: fenced rules block {index + 1} is not valid JSON ({exc})")
            continue
        found, found_problems = parse_rules(payload, source=source)
        rules.extend(found)
        problems.extend(found_problems)
    return rules, problems


class Interceptors:
    """The rule registry the orchestrator consults before every tool call."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._rules: list[InterceptorRule] = []
        self._errors: list[str] = []

    # ------------------------------------------------------------------ loading

    def load(
        self,
        *,
        genome_markdown: str = "",
        skill_rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace the rule set. Called at startup and whenever the sources change on disk."""
        rules: list[InterceptorRule] = []
        errors: list[str] = []

        genome_rules, genome_errors = extract_rules_from_markdown(genome_markdown, source="genome")
        rules.extend(genome_rules)
        errors.extend(genome_errors)

        for name, payload in (skill_rules or {}).items():
            source = f"skill:{name}"
            if isinstance(payload, str):
                found, found_errors = extract_rules_from_markdown(payload, source=source)
            else:
                found, found_errors = parse_rules(payload, source=source)
            rules.extend(found)
            errors.extend(found_errors)

        # Highest priority first, then declaration order for ties, so behaviour is stable.
        rules.sort(key=lambda rule: -rule.priority)
        ids = {rule.id for rule in rules}
        self._rules = rules
        self._errors = errors
        for problem in errors:
            logger.warning("interceptor rule problem: %s", problem)
        return {"rules": len(rules), "errors": errors, "ids": sorted(ids)}

    def load_genome_file(self, path: Path) -> dict[str, Any]:
        """Convenience wrapper: read GENOME.md if it exists, then load."""
        text = ""
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._errors = [f"genome: could not read {path} ({exc})"]
            return {"rules": len(self._rules), "errors": self._errors, "ids": [r.id for r in self._rules]}
        return self.load(genome_markdown=text)

    # ------------------------------------------------------------------ views

    @property
    def rules(self) -> list[InterceptorRule]:
        return list(self._rules)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def summarise(self) -> dict[str, Any]:
        by_source: dict[str, int] = {}
        for rule in self._rules:
            by_source[rule.source] = by_source.get(rule.source, 0) + 1
        return {
            "enabled": self.enabled,
            "count": len(self._rules),
            "by_source": dict(sorted(by_source.items())),
            "errors": self._errors,
            "rules": [rule.as_dict() for rule in self._rules],
        }

    # ------------------------------------------------------------------ evaluation

    def evaluate(self, tool: str, params: dict[str, Any] | None = None) -> InterceptorOutcome:
        """Evaluate every rule against one tool call.

        All matching rules are reported, not just the winner, so the UI can explain exactly which
        axioms fired and why.
        """
        outcome = InterceptorOutcome()
        if not self.enabled or not self._rules:
            return outcome
        payload = params if isinstance(params, dict) else {}
        for rule in self._rules:
            try:
                matched = rule.matches(tool, payload)
            except Exception as exc:  # noqa: BLE001 - a broken rule must not stop execution
                logger.warning("interceptor %s failed to evaluate (%s)", rule.id, exc)
                continue
            if not matched:
                continue
            outcome.fired.append(rule.id)
            conclusion = f"{rule.reason or rule.id} (rule {rule.id})"
            if rule.verdict is Verdict.BLOCK:
                outcome.verdict = Verdict.BLOCK
                outcome.reasons.append(conclusion)
            elif rule.verdict is Verdict.APPROVAL:
                outcome.verdict = stricter(outcome.verdict, Verdict.APPROVAL)
                outcome.reasons.append(conclusion)
            else:
                outcome.annotations.append(rule.reason or rule.id)
        return outcome


def build_interceptors(settings: Any) -> Interceptors:
    return Interceptors(enabled=bool(getattr(settings, "interceptors_enabled", True)))
