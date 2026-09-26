"""Risk classification.

Deterministic and testable: a tool's declared floor, escalated by patterns in the action's target and
parameters. The model never sets risk directly — it proposes an action, this module says how dangerous
it is, and the policy engine decides what to do about it.
"""

from __future__ import annotations

import re

from app.schemas import ActionSpec, RiskLevel

# Declared floors per tool. A tool can be escalated but never silently downgraded.
TOOL_RISK_FLOORS: dict[str, RiskLevel] = {
    # reads and research
    "tavily_search": RiskLevel.LOW,
    "tavily_research": RiskLevel.LOW,
    "research": RiskLevel.LOW,
    "fs_list": RiskLevel.LOW,
    "fs_read": RiskLevel.LOW,
    "browser_open": RiskLevel.LOW,
    "browser_read": RiskLevel.LOW,
    "screen_capture": RiskLevel.LOW,
    "screen_analyze": RiskLevel.LOW,
    "app_open": RiskLevel.LOW,
    "remember": RiskLevel.LOW,
    "memory_write": RiskLevel.LOW,
    "task_create": RiskLevel.LOW,
    "task_update": RiskLevel.LOW,
    # creates and reversible changes
    "fs_mkdir": RiskLevel.MEDIUM,
    "fs_write": RiskLevel.MEDIUM,
    "fs_move": RiskLevel.MEDIUM,
    "browser_click": RiskLevel.MEDIUM,
    "browser_type": RiskLevel.MEDIUM,
    "computer_click": RiskLevel.MEDIUM,
    "computer_type": RiskLevel.MEDIUM,
    "computer_scroll": RiskLevel.MEDIUM,
    "computer_key": RiskLevel.MEDIUM,
    "computer_drag": RiskLevel.MEDIUM,
    "skill_run": RiskLevel.MEDIUM,
    "automation_create": RiskLevel.MEDIUM,
    "reminder_create": RiskLevel.LOW,
    "terminal_run": RiskLevel.MEDIUM,
    # dangerous
    "fs_delete": RiskLevel.HIGH,
    "browser_submit": RiskLevel.HIGH,
    "message_send": RiskLevel.HIGH,
    "email_send": RiskLevel.HIGH,
    "package_install": RiskLevel.HIGH,
    "credential_change": RiskLevel.CRITICAL,
    "purchase": RiskLevel.CRITICAL,
    "financial_transaction": RiskLevel.CRITICAL,
}

# Escalation patterns, checked against the flattened action text.
_CRITICAL_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\b(purchase|buy now|checkout|place order|pay\b|payment|wire transfer|bitcoin|invoice)\b",
     "action looks financial"),
    (r"(?i)\b(password|passwd|api[_-]?key|secret|token|credential|ssh key|private key)",
     "action touches credentials"),
    (r"(?i)\.env\b", "action touches the secret file"),
    (r"(?i)\b(delete (my )?account|close account|change (billing|2fa|two-factor)|recovery codes?)\b",
     "action changes account security"),
    (r"(?i)\b(secure erase|shred|wipe (disk|drive)|format [a-z]:|diskpart|mkfs|cipher /w)\b",
     "irreversible destruction"),
]

_HIGH_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\b(delete|remove|unlink|erase|rm\b|rmdir|del\b|trash)\b", "action deletes data"),
    (r"(?i)\b(send|submit|post|publish|tweet|email|dm\b|transfer)\b", "action sends something out"),
    (r"(?i)(power ?shell|cmd\.exe|\.exe\b|registry|reg add|schtasks|services\.msc|net user)", "shell/registry access"),
    (r"(?i)\b(\.env\b|id_rsa|credentials|keystore|wallet\.dat|\.ssh|system32|/etc/|program files)", "sensitive target"),
]

_MEDIUM_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)\b(install|uninstall|upgrade|pip install|npm i\b|winget|choco|apt-get)\b", "installs software"),
    (r"(?i)\b(overwrite|replace|move|rename|chmod|chown|takeown|icacls)\b", "modifies existing data"),
    (r"(?i)\b(login|sign in|authenticate|oauth|cookie|session)\b", "touches authentication state"),
]

# Credential-ish parameter keys: their *presence* raises the floor regardless of tool.
_CREDENTIAL_KEYS = re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token|private[_-]?key)")


def flatten_action(action: ActionSpec) -> str:
    """Render an action into one searchable string for pattern matching.

    Tool names are normalised (``fs_delete`` → ``fs delete``) so verb patterns match the semantics of
    the tool as well as its arguments — otherwise ``fs_delete`` would slip past a ``\\bdelete\\b`` rule.
    """
    parts = [action.tool.replace("_", " "), action.description.replace("_", " "), action.expected]
    for key, value in action.params.items():
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}")
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key}=" + ",".join(str(item) for item in value[:20]))
        elif isinstance(value, dict):
            parts.append(f"{key}=" + ",".join(f"{k}:{v}" for k, v in list(value.items())[:10]))
    return " ".join(str(part) for part in parts)


def _escalate(current: RiskLevel, candidate: RiskLevel) -> RiskLevel:
    return candidate if candidate.rank > current.rank else current


def classify(action: ActionSpec) -> tuple[RiskLevel, list[str]]:
    """Return the risk level for an action plus the reasons that raised it."""
    text = flatten_action(action)
    risk = action.risk_floor or TOOL_RISK_FLOORS.get(action.tool, RiskLevel.MEDIUM)
    reasons: list[str] = []

    for pattern, reason in _CRITICAL_PATTERNS:
        if re.search(pattern, text):
            risk = _escalate(risk, RiskLevel.CRITICAL)
            reasons.append(reason)
    for pattern, reason in _HIGH_PATTERNS:
        if re.search(pattern, text):
            risk = _escalate(risk, RiskLevel.HIGH)
            reasons.append(reason)
    for pattern, reason in _MEDIUM_PATTERNS:
        if re.search(pattern, text):
            risk = _escalate(risk, RiskLevel.MEDIUM)
            reasons.append(reason)

    for key in action.params:
        if _CREDENTIAL_KEYS.search(str(key)):
            risk = _escalate(risk, RiskLevel.CRITICAL)
            reasons.append("argument carries a credential")

    if action.tool in {"fs_write", "fs_move"} and action.reversible is False:
        risk = _escalate(risk, RiskLevel.HIGH)
        reasons.append("write is not reversible")

    # Bulk operations on many targets are riskier than their unit action.
    count = action.params.get("count") or 0
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    if count > 20:
        risk = _escalate(risk, RiskLevel.HIGH)
        reasons.append(f"affects {count} targets")
    elif count > 5:
        risk = _escalate(risk, RiskLevel.MEDIUM)
        reasons.append(f"affects {count} targets")

    if not reasons:
        reasons.append("no escalation patterns matched")
    return risk, reasons


def requires_approval(risk: RiskLevel) -> bool:
    """HIGH and CRITICAL actions are human-gated; CRITICAL is never automatically approved."""
    return risk.rank >= RiskLevel.HIGH.rank


def describe(action: ActionSpec, risk: RiskLevel) -> str:
    target = (
        action.params.get("path")
        or action.params.get("url")
        or action.params.get("command")
        or action.params.get("query")
        or ""
    )
    base = action.description or f"{action.tool} on {target}" if target else action.tool
    return f"[{risk.value}] {base}"
