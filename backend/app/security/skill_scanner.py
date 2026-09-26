"""Skill Scanner — a pre-activation scan for the ``skills/`` folder.

A skill is not a tool. A tool has a fixed signature and a declared risk floor; a skill is *prose and
a workflow* that the model reads as guidance. That makes a skill file a legitimate attack surface:
prompt injection, a hardcoded credential, a step that ships data somewhere, or an obfuscated shell
command hiding in a "cleanup" workflow. The use-case collections in the wild say the same thing —
third-party skills are unaudited, so review them before you trust them.

This module is that review, automated and deterministic. It runs when skills are loaded, and the
result decides whether a skill may be offered to the planner at all.

Modes (``SKILL_SCAN_MODE``):

* ``block`` — a critical finding refuses the skill.
* ``warn`` — findings are reported, but nothing is refused.
* ``off`` — no scanning.

``SKILL_SCAN_ALLOWLIST`` names skills that are trusted regardless of findings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.security.policies import DESTRUCTIVE_COMMAND_PATTERNS, SHELL_EVASION_BLOCK_PATTERNS

CRITICAL = "critical"
HIGH = "high"
MEDIUM = "medium"

# --- rules ---------------------------------------------------------------------

# Instructions aimed at the model rather than at the task. A skill legitimately describes *what* to
# do; it has no business telling the assistant to change its own rules or to hide things from the user.
_PROMPT_INJECTION: tuple[tuple[str, str], ...] = (
    (r"(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts)", "tells the assistant to ignore its instructions"),
    (r"(?i)disregard\s+(all\s+)?(previous|prior|above|safety)", "tells the assistant to disregard its rules"),
    (r"(?i)(you are now|from now on you are|act as if you are|pretend to be)\b", "tries to replace the assistant's identity"),
    (r"(?i)(new|updated)\s+system\s+prompt", "tries to install a new system prompt"),
    (r"(?i)do not tell the (user|human)|don'?t tell the (user|human)|without the user knowing|hide this from", "asks the assistant to conceal its actions"),
    (r"(?i)(without asking|don'?t ask|no need to ask|skip (the )?approval|auto-?approve|bypass (the )?(policy|approval|guard))", "tries to remove the approval gate"),
    (r"(?i)(override|ignore)\s+(your\s+)?(safety|policies|guardrails|restrictions)", "tries to override the safety layer"),
    (r"(?i)\bjailbreak\b|\bDAN\s+mode\b", "contains a jailbreak instruction"),
)

# Credentials committed into the skill itself. Rotating a key that shipped in a skill file is the
# only real remedy, so this is critical rather than a warning.
_SECRETS: tuple[tuple[str, str], ...] = (
    (r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI-style API key"),
    (r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b", "Anthropic API key"),
    (r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b", "GitHub token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"\bAIza[0-9A-Za-z\-_]{35}\b", "Google API key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "JSON Web Token"),
    (r"(?i)\b(api[_-]?key|secret|password|passwd|token|bearer)\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-]{16,}", "credential assigned to a literal value"),
)

# A step that moves data out of the machine. Named webhooks and chat APIs first, because those are
# one-line exfiltration and have no other reason to appear in a skill.
_EXFILTRATION: tuple[tuple[str, str], ...] = (
    (r"(?i)https?://(discord(app)?\.com/api/webhooks|hooks\.slack\.com|api\.telegram\.org/bot|webhook\.site|pipedream\.net|requestbin)", "posts to a webhook that is not yours"),
    (r"(?i)(curl|wget|invoke-webrequest|iwr|requests\.post|httpx\.post|fetch\()\b[^\n]{0,200}(file|content|env|key|token|password)", "uploads a local file or credential over the network"),
    (r"(?i)base64[^\n|]*\|\s*(curl|wget|nc|ncat)", "sends a base64-encoded payload out"),
    (r"(?i)\b(scp|rsync)\b[^\n]{0,120}@[^\s]+:", "copies data to a remote host"),
)

# Reused verbatim from the policy layer rather than restated here. Both modules must agree on what
# "dangerous" means: an earlier hand-copied version of this list used a bare `\bformat\b`, which
# matched the word "format" in ordinary prose and blocked Dobot's own weekly_report skill. One
# source of truth removes that whole class of bug.
_DANGEROUS_COMMAND: tuple[tuple[str, str], ...] = tuple(
    [(pattern, "matches a destructive command pattern") for pattern in DESTRUCTIVE_COMMAND_PATTERNS]
    + list(SHELL_EVASION_BLOCK_PATTERNS)
    + [(r"(?i)\bchmod\s+777\b", "makes a target world-writable")]
)

# Tools a workflow may name. Kept in sync with the registry's declared floors; a step referencing
# anything else cannot be risk-classified, so it is reported rather than silently allowed.
KNOWN_TOOLS: frozenset[str] = frozenset(
    {
        "tavily_search",
        "tavily_research",
        "deep_research",
        "research",
        "fs_list",
        "fs_read",
        "fs_write",
        "fs_mkdir",
        "fs_move",
        "fs_delete",
        "browser_open",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_submit",
        "screen_capture",
        "screen_analyze",
        "app_open",
        "remember",
        "memory_write",
        "task_create",
        "task_update",
        "terminal_run",
        "computer_click",
        "computer_type",
        "computer_scroll",
        "computer_key",
        "computer_drag",
        "skill_run",
        "automation_create",
        "reminder_create",
        "message_send",
        "email_send",
        "package_install",
    }
)

_ABSOLUTE_PATH = re.compile(r"(?i)^([a-z]:[\\/]|/|\\\\)")


@dataclass
class Finding:
    rule: str
    severity: str
    detail: str
    location: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "detail": self.detail,
            "location": self.location,
        }


@dataclass
class ScanReport:
    name: str
    status: str = "clean"  # clean | warn | blocked | allowlisted | off
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _scan_text(
    text: str,
    location: str,
    rules: tuple[tuple[str, str], ...],
    *,
    rule: str,
    severity: str,
    findings: list[Finding],
) -> None:
    for pattern, detail in rules:
        if re.search(pattern, text):
            findings.append(Finding(rule=rule, severity=severity, detail=detail, location=location))


def scan_workflow_steps(steps: list[dict], location: str, findings: list[Finding]) -> None:
    """Inspect each machine-readable step: its tool, and every string argument it carries."""
    for index, step in enumerate(steps, start=1):
        where = f"{location}:step{index}"
        tool = str(step.get("tool") or step.get("action") or "").strip()
        if tool and tool not in KNOWN_TOOLS:
            findings.append(
                Finding(
                    rule="unknown_tool",
                    severity=MEDIUM,
                    detail=f"step names a tool the registry does not declare: {tool}",
                    location=where,
                )
            )
        for key, value in step.items():
            if not isinstance(value, str):
                continue
            lowered = key.lower()
            if "command" in lowered or "cmd" in lowered:
                _scan_text(
                    value, where, _DANGEROUS_COMMAND, rule="dangerous_command",
                    severity=CRITICAL, findings=findings,
                )
            if "path" in lowered and _ABSOLUTE_PATH.match(value.strip()):
                findings.append(
                    Finding(
                        rule="absolute_path",
                        severity=MEDIUM,
                        detail=f"step hardcodes an absolute path: {value[:120]}",
                        location=where,
                    )
                )


def scan_skill(
    name: str,
    directory: Path,
    *,
    mode: str = "block",
    allowlist: tuple[str, ...] | list[str] = (),
) -> ScanReport:
    """Scan one skill directory. Deterministic, offline, and safe to run on every load."""
    if mode == "off":
        return ScanReport(name=name, status="off")
    if name in set(allowlist):
        return ScanReport(name=name, status="allowlisted")

    findings: list[Finding] = []
    skill_file = directory / "SKILL.md"
    workflow_file = directory / "workflow.json"

    if skill_file.exists():
        try:
            body = skill_file.read_text(encoding="utf-8")
        except OSError:
            body = ""
        _scan_text(body, "SKILL.md", _PROMPT_INJECTION, rule="prompt_injection",
                   severity=CRITICAL, findings=findings)
        _scan_text(body, "SKILL.md", _SECRETS, rule="hardcoded_secret",
                   severity=CRITICAL, findings=findings)
        _scan_text(body, "SKILL.md", _EXFILTRATION, rule="exfiltration",
                   severity=CRITICAL, findings=findings)
        _scan_text(body, "SKILL.md", _DANGEROUS_COMMAND, rule="dangerous_command",
                   severity=CRITICAL, findings=findings)

    if workflow_file.exists():
        import json

        try:
            payload = json.loads(workflow_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            findings.append(
                Finding(
                    rule="invalid_workflow",
                    severity=HIGH,
                    detail="workflow.json could not be parsed, so its steps cannot be reviewed",
                    location="workflow.json",
                )
            )
            payload = None
        if payload is not None:
            steps = payload.get("steps", payload) if isinstance(payload, dict) else payload
            raw = json.dumps(steps, ensure_ascii=False)
            _scan_text(raw, "workflow.json", _SECRETS, rule="hardcoded_secret",
                       severity=CRITICAL, findings=findings)
            _scan_text(raw, "workflow.json", _EXFILTRATION, rule="exfiltration",
                       severity=CRITICAL, findings=findings)
            _scan_text(raw, "workflow.json", _PROMPT_INJECTION, rule="prompt_injection",
                       severity=CRITICAL, findings=findings)
            if isinstance(steps, list):
                scan_workflow_steps([step for step in steps if isinstance(step, dict)], "workflow.json", findings)

    critical = any(finding.severity == CRITICAL for finding in findings)
    if not findings:
        return ScanReport(name=name, status="clean")
    if mode == "block" and critical:
        return ScanReport(name=name, status="blocked", findings=findings)
    return ScanReport(name=name, status="warn", findings=findings)
