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
    # The credential rule is *shape*-based (see CREDENTIAL_SHAPE): a bare "password" in a grep
    # pattern or a file called secret_santa.txt is ordinary work, and gating it every time trains
    # the user to approve without reading.
    (r"(?i)\b(delete (my )?account|close account|change (billing|2fa|two-factor)|recovery codes?)\b",
     "action changes account security"),
    (r"(?i)\b(secure erase|shred|wipe (disk|drive)|format [a-z]:|diskpart|mkfs|cipher /w)\b",
     "irreversible destruction"),
]

#: A credential being *handled*, not merely named: an assignment, a flag with a value, a shell
#: environment expansion, an authorization header, or a literal key. This one pattern is shared by
#: the risk classifier and the policy engine so the two can never disagree about what counts.
#:
#: What it deliberately does not match: `grep password app.py`, `type passwords.txt`, a file called
#: `secret_santa.txt`, a commit message about rotating tokens. Those are ordinary work; a rule that
#: blocks them makes every real prompt look like noise.
CREDENTIAL_WORD = (
    r"(?:password|passwd|pwd|api[_-]?key|access[_-]?key|secret|token|credentials?|"
    r"private[_-]?key|ssh[_-]?key|recovery codes?|2fa)"
)
CREDENTIAL_SHAPE = re.compile(
    r"(?i)"
    # key=value / key: value — `api_key=abcdef123456`, `Authorization: Bearer …`
    + CREDENTIAL_WORD + r"\s*[=:]\s*\S{4,}"
    # --password hunter2 / -token xyz (flag with a value)
    + r"|--(?:password|passwd|pwd|token|secret|api[_-]?key|credential|private[_-]?key)\s+\S{4,}"
    # $API_KEY, ${MY_PASSWORD}, %AWS_SECRET_ACCESS_KEY% — the value comes from the environment
    + r"|\$\{?(?:[A-Za-z0-9_]+_)?" + CREDENTIAL_WORD + r"(?:_[A-Za-z0-9]+)?\}?"
    + r"|%\{?(?:[A-Za-z0-9_]+_)?" + CREDENTIAL_WORD + r"(?:_[A-Za-z0-9]+)?\}?%"
    # header tokens and well-known key shapes
    + r"|bearer\s+[a-z0-9\-._~+/]{12,}"
    + r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"
    + r"|\bAKIA[0-9A-Z]{16}\b"
    + r"|\bsk-[A-Za-z0-9]{20,}\b"
    + r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

#: Credential-*ish file names. Checked only against path arguments: a path that names a credential
#: store deserves the human gate even without a value attached (`secrets.json`, `passwords.txt`).
CREDENTIAL_FILE_NAME = re.compile(
    r"(?i)(\.env\b|id_(rsa|dsa|ecdsa|ed25519)|credentials?\.(json|txt|csv|ya?ml)|"
    r"secrets?\.(json|txt|ya?ml)|passwords?\.(json|txt|csv|ya?ml)|keystore|\.pem$|\.pfx$)"
)

_PATH_KEYS_FOR_CREDENTIALS = ("path", "target", "destination", "source", "cwd", "directory", "paths")


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

    # Credentials: a handled value anywhere (shape), or a credential file named by a path argument.
    # Free text that merely *mentions* a credential stays at its declared floor.
    if CREDENTIAL_SHAPE.search(text):
        risk = _escalate(risk, RiskLevel.CRITICAL)
        reasons.append("action handles a credential value")
    else:
        path_text = " ".join(
            str(action.params.get(key))
            for key in _PATH_KEYS_FOR_CREDENTIALS
            if action.params.get(key) is not None
        )
        if re.search(r"(?i)\.env\b", path_text):
            risk = _escalate(risk, RiskLevel.CRITICAL)
            reasons.append("action touches the secret file")
        elif CREDENTIAL_FILE_NAME.search(path_text):
            risk = _escalate(risk, RiskLevel.CRITICAL)
            reasons.append("path names a credential file")

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
