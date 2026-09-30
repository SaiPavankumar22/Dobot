"""Deterministic policy engine.

Hard safety rules that must not depend on a model's judgement. Policies are data: each one matches a
shape of action and returns ALLOW / APPROVAL / BLOCK plus a reason. A single BLOCK anywhere
short-circuits the whole plan.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from app.schemas import ActionSpec, RiskLevel, Verdict
from app.security.risk import CREDENTIAL_SHAPE

# Paths that are never writable, no matter who asks.
FORBIDDEN_WRITE_PREFIXES = (
    "c:/windows",
    "c:/program files",
    "c:/program files (x86)",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/system",
    "/library",
    "/private/var",
)

DESTRUCTIVE_COMMAND_PATTERNS = (
    r"(?i)rm\s+-rf\s+/(?:\s|$)",
    r"(?i)rm\s+-rf\s+[a-z]:[\\/]?\s*$",
    r"(?i)\bformat\s+[a-z]:",
    r"(?i)\bdiskpart\b",
    r"(?i)\bmkfs\b",
    r"(?i)dd\s+if=.*of=/dev/[a-z]",
    r"(?i)del\s+/[sq]\s+[a-z]:\\",
    r"(?i)\b(shutdown|reboot)\s+/[rf]",
    r"(?i):\(\)\s*\{.*\};:",  # fork bomb
    r"(?i)\bvssadmin\s+delete\s+shadows",
    r"(?i)\bcipher\s+/w",
    r"(?i)\bnet\s+user\b.*\b(add|delete)\b",
    r"(?i)\breg\s+(add|delete)\b.*HKLM",
)

# Read-only or clearly safe commands: still routed through the sandbox, but not a policy concern.
SAFE_COMMANDS = (
    "ls", "dir", "pwd", "cd", "cat", "type", "echo", "git", "python", "node", "npm", "pytest",
    "code", "where", "which", "head", "tail", "wc", "grep", "find", "rg", "tree", "stat", "file",
)


@dataclass
class PolicyContext:
    allowed_paths: list[Path] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    sandbox_provider: str = "local"
    shadow_mode: bool = False
    user_message: str = ""
    autonomy: str = "assisted"  # assisted | autonomous
    #: When set, every tool that *changes* data needs explicit approval even at MEDIUM risk.
    require_write_approval: bool = False
    #: Execution mode for this request: ask | assist | agent. Sets the approval threshold.
    mode: str = "agent"
    #: Locations the file guard refuses to read or write, whatever the mode or approval state.
    protected_paths: list[Path] = field(default_factory=list)
    #: Skills the scanner flagged. A blocked skill can never be run.
    blocked_skills: list[str] = field(default_factory=list)


# --- file guard --------------------------------------------------------------
#
# An independent guard, not part of the command guards: a sensitive *location* is off limits to tools
# even for reading, because reading a private key is a credential disclosure, not a side effect. This
# is deliberately independent of SANDBOX_ALLOWED_PATHS, which is often the whole home directory.
PROTECTED_NAME_PATTERNS: tuple[str, ...] = (
    r"(?i)^\.env(\..+)?$",  # .env, .env.local
    r"(?i)^id_(rsa|dsa|ecdsa|ed25519)$",
    r"(?i)^credentials(\.json)?$",
    r"(?i)^(keystore|keychain|login\.keychain)(\..+)?$",
    r"(?i)^wallet\.dat$",
    r"(?i)\.(p12|pfx|jks|keystore|ppk)$",
    r"(?i)^(cookies|login data|web data)$",
    r"(?i)^(_netrc|\.netrc)$",
)

#: Bare filenames worth flagging inside a shell command even without a full path.
_PROTECTED_BASENAMES: tuple[str, ...] = (
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "wallet.dat",
    "login.keychain",
)

# --- shell evasion ------------------------------------------------------------
#
# A literal destructive-command list is easy to walk around. These rules look for the *shape* of an
# evasion attempt: encoding a payload, fetching and executing, opening a reverse shell, unsetting the
# audit trail, or planting persistence. Two tiers, because intent differs: some shapes have no honest
# use in an assistant, others are merely suspicious and should be confirmed.
SHELL_EVASION_BLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)(base64|openssl\s+enc)\s+(-d|--decode)[^|]*\|\s*(ba|z|da)?sh", "decodes a payload and pipes it into a shell"),
    (r"(?i)-enc(odedcommand)?\s+[A-Za-z0-9+/=]{24,}", "runs a PowerShell command from a base64 blob"),
    (r"(?i)frombase64string\s*\(", "builds a command from base64"),
    (r"(?i)(curl|wget|iwr|invoke-webrequest)[^|;&]*\|\s*(ba|z|da)?sh\b", "pipes a download straight into a shell"),
    (r"(?i)(iwr|invoke-webrequest|curl|wget)[^;|&]*\|\s*iex\b", "pipes a download into Invoke-Expression"),
    (r"(?i)\b(nc|ncat|netcat)\b[^;|&]*-e\s", "opens a reverse shell with netcat"),
    (r"/dev/tcp/\d{1,3}(\.\d{1,3}){3}/\d+", "opens a raw socket via /dev/tcp"),
    (r"(?i)bash\s+-i\s*>\s*&\s*/dev/tcp", "spawns an interactive reverse shell"),
    (r"(?i)\bsocat\b[^;|&]*\bexec:", "relays a shell through socat"),
    (r"(?i)(certutil|bitsadmin)\b[^;|&]*-decode", "decodes a payload with a Windows tool"),
    (r"(?i)(history\s+-c|unset\s+HISTFILE|wevtutil\s+cl|clear-eventlog)", "erases the audit trail"),
    (r"(?i)(schtasks\s+/create|reg\s+add[^;|&]*\\run\b|systemctl\s+enable\b|crontab\s+-\b)", "plants persistence"),
    (r"(?i)\.ssh/id_(rsa|ed25519)\b.*(curl|nc|base64|python)", "transmits a private key"),
)

SHELL_EVASION_SUSPICIOUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)\beval\s*\(", "evaluates a string as code"),
    (r"(?i)\$ifs|\$\{ifs\}", "splits words with $IFS to defeat text matching"),
    (r"(?i)\\x[0-9a-f]{2}\\x[0-9a-f]{2}", "uses hex escapes to obfuscate a command"),
    (r"(?i)echo\s+[A-Za-z0-9+/=]{40,}\s*\|", "pipes an encoded blob"),
    (r"(?i)(chmod\s+\+x|icacls[^;|&]*\/grant)[^;|&]*&&\s*\./", "makes a file executable and runs it"),
    (r"(?i)\bpython[0-9.]*\s+-c\s+['\"]import\s+socket", "opens a socket from an inline script"),
    (r"(?i)set\s+-\s*(x|v)\b", "enables shell tracing"),
)

# Tools that change the world rather than inspect it. Used by the write-approval policy below.
# `skill_run` is here deliberately: an unexpanded skill executes its whole workflow under a single
# decision, so leaving it out would let a skill move files without ever being gated.
MUTATING_TOOLS: tuple[str, ...] = (
    "fs_write",
    "fs_mkdir",
    "fs_move",
    "fs_delete",
    "terminal_run",
    "package_install",
    "skill_run",
    "computer_click",
    "computer_type",
    "computer_key",
    "computer_drag",
)


@dataclass
class PolicyOutcome:
    policy: str
    verdict: Verdict
    reason: str
    escalate_to: RiskLevel | None = None
    #: True when this refusal may be turned into a question: the user can grant the scope once or
    #: for good instead of being told no. Only policies that guard a *location* the user owns are
    #: grantable — credential stores, destructive commands and system directories never are.
    grantable: bool = False


@dataclass
class Policy:
    name: str
    verdict: Verdict
    reason: str
    tools: tuple[str, ...] = ()
    predicate: Callable[[ActionSpec, PolicyContext], bool] | None = None
    escalate_to: RiskLevel | None = None
    description: str = ""
    #: Refusals the user may override with a one-time or lifetime permission (see grants.py).
    grantable: bool = False

    def matches(self, action: ActionSpec, ctx: PolicyContext) -> bool:
        if self.tools and action.tool not in self.tools:
            return False
        if self.predicate is None:
            return True
        try:
            return bool(self.predicate(action, ctx))
        except Exception:  # noqa: BLE001 - a broken predicate must not silently allow an action
            return False


def _action_text(action: ActionSpec) -> str:
    parts = [action.tool, action.description, action.expected]
    for key, value in action.params.items():
        parts.append(f"{key}={value}")
    return " ".join(str(part) for part in parts)


_PATH_KEYS = ("path", "target", "destination", "source", "cwd", "directory", "paths", "sources")
_GLOB_CHARS = re.compile(r"[*?\[\]{}]")


def _all_paths(action: ActionSpec) -> list[str]:
    """Every path-like argument in an action, with glob characters stripped.

    Containment must consider *all* of them: an action that reads inside the workspace and writes
    outside it is exactly the case a single-parameter check would miss.
    """
    found: list[str] = []
    for key in _PATH_KEYS:
        value = action.params.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(item) for item in value if str(item).strip())
    cleaned = [_GLOB_CHARS.sub("", item).strip() for item in found]
    return [item for item in cleaned if item]


def _path_param(action: ActionSpec) -> str | None:
    paths = _all_paths(action)
    return paths[0] if paths else None


def _url_host(action: ActionSpec) -> str | None:
    for key in ("url", "endpoint", "host"):
        value = action.params.get(key)
        if isinstance(value, str) and value.strip():
            parsed = urlparse(value if "://" in value else f"https://{value}")
            return (parsed.hostname or "").lower() or None
    return None


def resolve_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve(strict=False)


def is_inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def is_forbidden_write(path: Path) -> bool:
    text = path.as_posix().lower()
    return any(
        text == prefix or text.startswith(prefix + "/") for prefix in FORBIDDEN_WRITE_PREFIXES
    )


def _command(action: ActionSpec) -> str:
    value = action.params.get("command") or action.params.get("cmd") or ""
    return str(value)


def _count(action: ActionSpec) -> int:
    try:
        return int(action.params.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _name_is_protected(name: str) -> bool:
    return any(re.match(pattern, name) for pattern in PROTECTED_NAME_PATTERNS)


def is_protected_name(name: str) -> bool:
    """Public wrapper: the sandbox uses this so a grant can never unlock a credential file."""
    return _name_is_protected(name)


def outside_roots(action: ActionSpec, ctx: PolicyContext) -> list[str]:
    """Folder roots this action touches *outside* the allowed workspace — the scope to ask about.

    A file resolves to its containing folder, a directory to itself, so a grant means "this folder",
    never "the whole drive".
    """
    if not ctx.allowed_paths:
        return []
    roots: list[str] = []
    for raw in _all_paths(action):
        path = resolve_path(raw)
        if is_inside(path, ctx.allowed_paths):
            continue
        root = path if path.is_dir() else path.parent
        text = root.as_posix()
        if text not in roots:
            roots.append(text)
    return roots


def _touches_protected(action: ActionSpec, ctx: PolicyContext) -> bool:
    """True when any path argument, or a shell command, names a protected location.

    Two matchers on purpose. The path matcher catches tools that carry a real path; the text matcher
    catches a shell command that merely mentions one, so ``cat ~/.ssh/id_rsa`` is caught even though
    ``fs_read`` never sees the file.
    """
    if not ctx.protected_paths:
        return False

    for raw in _all_paths(action):
        path = resolve_path(raw)
        if is_inside(path, ctx.protected_paths) or _name_is_protected(path.name):
            return True

    command = _command(action)
    if not command:
        return False
    flattened = command.replace("\\", "/").lower()

    # Compare against both spellings of every protected root. A command is written the way a person
    # would write it — `cat ~/.ssh/id_rsa` — while the roots are resolved absolute paths, so matching
    # only the absolute form silently misses the common case.
    home = Path.home()
    needles: set[str] = set()
    for root in ctx.protected_paths:
        needles.add(root.as_posix().lower())
        try:
            needles.add((Path("~") / root.relative_to(home)).as_posix().lower())
        except ValueError:
            continue
    if any(needle and needle in flattened for needle in needles):
        return True

    # Finally, a bare filename anywhere in the command, so "type .env" and "/tmp/x/id_rsa" are both
    # caught. Separators count as boundaries here: the interesting cases are path suffixes.
    return any(
        re.search(rf"(?i)(^|[\s\"'/\\]){re.escape(name)}([\s\"']|$)", command)
        for name in _PROTECTED_BASENAMES
    )


def _targets(action: ActionSpec) -> list[str]:
    value = action.params.get("paths") or action.params.get("targets") or action.params.get("sources") or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    path = _path_param(action)
    return [path] if path else []


# ------------------------------------------------------------------ builtin policies

BUILTIN_POLICIES: list[Policy] = [
    Policy(
        name="protected_path_blocked",
        verdict=Verdict.BLOCK,
        reason="Target is a protected credential location; the file guard blocks reads as well as writes",
        tools=(
            "fs_read",
            "fs_list",
            "fs_write",
            "fs_move",
            "fs_mkdir",
            "fs_delete",
            "terminal_run",
            "computer_type",
        ),
        predicate=_touches_protected,
        description=(
            "Independent of the command guards and of SANDBOX_ALLOWED_PATHS: credential stores are "
            "never read or written by a tool, whatever the mode or approval state."
        ),
    ),
    Policy(
        name="shell_evasion_blocked",
        verdict=Verdict.BLOCK,
        reason="Command shape indicates deliberate evasion (encoded payload, download-and-run, reverse shell, audit tampering)",
        tools=("terminal_run", "computer_type", "skill_run"),
        predicate=lambda action, _ctx: bool(
            any(re.search(pattern, _command(action)) for pattern, _ in SHELL_EVASION_BLOCK_PATTERNS)
        ),
        description="Looking for the shape of an evasion attempt, not just a literal dangerous verb.",
    ),
    Policy(
        name="shell_evasion_suspicious",
        verdict=Verdict.APPROVAL,
        reason="Command shape is obfuscated or has no honest use in an assistant; showing it to the user before running",
        tools=("terminal_run", "computer_type", "skill_run"),
        predicate=lambda action, _ctx: bool(
            any(
                re.search(pattern, _command(action))
                for pattern, _ in SHELL_EVASION_SUSPICIOUS_PATTERNS
            )
        ),
        escalate_to=RiskLevel.HIGH,
    ),
    Policy(
        name="blocked_skill_refused",
        verdict=Verdict.BLOCK,
        reason="The skill scanner flagged this skill; it cannot run until you review it",
        tools=("skill_run",),
        predicate=lambda action, ctx: bool(
            ctx.blocked_skills
            and str(action.params.get("name") or action.params.get("skill") or "") in ctx.blocked_skills
        ),
        description="A skill the scanner flagged never executes, whatever its risk floor.",
    ),
    Policy(
        name="writes_require_approval",
        verdict=Verdict.APPROVAL,
        reason="Everything that changes data is confirmed first (REQUIRE_WRITE_APPROVAL)",
        tools=MUTATING_TOOLS,
        predicate=lambda _action, ctx: ctx.require_write_approval,
        description=(
            "Opt-in strictness: with REQUIRE_WRITE_APPROVAL on, no mutating tool runs without an "
            "approval, even at MEDIUM risk. Read-only tools are unaffected."
        ),
    ),
    Policy(
        name="destructive_command_blocked",
        verdict=Verdict.BLOCK,
        reason="Command matches a destructive pattern (disk/format/recursive root delete/system change)",
        tools=("terminal_run", "computer_type", "skill_run"),
        predicate=lambda action, _ctx: any(
            re.search(pattern, _command(action)) for pattern in DESTRUCTIVE_COMMAND_PATTERNS
        ),
        description="Irreversible system destruction never executes, even with approval.",
    ),
    Policy(
        name="no_credentials_in_command",
        verdict=Verdict.BLOCK,
        reason="Refusing to pass a credential value through a shell/automation command",
        tools=("terminal_run", "computer_type", "browser_type", "message_send", "email_send"),
        predicate=lambda action, _ctx: bool(CREDENTIAL_SHAPE.search(_action_text(action))),
        description=(
            "Matches a credential *shape* — an assignment, a flag with a value, an environment "
            "expansion, a bearer token, a literal key — not the bare word. `grep password app.py` "
            "and a file named secret_santa.txt are ordinary work."
        ),
    ),
    Policy(
        name="path_outside_sandbox",
        verdict=Verdict.BLOCK,
        reason=(
            "Target path is outside the allowed workspace roots — you can allow it once "
            "or always"
        ),
        tools=("fs_write", "fs_delete", "fs_move", "fs_mkdir", "fs_read", "fs_list", "terminal_run"),
        predicate=lambda action, ctx: bool(
            ctx.allowed_paths
            and any(
                not is_inside(resolve_path(raw), ctx.allowed_paths)
                for raw in _all_paths(action)
            )
        ),
        grantable=True,
        description=(
            "The user's own files are not Dobot's to forbid: a refusal here becomes a permission "
            "question (once / always), and the answer is remembered per folder."
        ),
    ),
    Policy(
        name="system_paths_read_only",
        verdict=Verdict.BLOCK,
        reason="System directories are never writable",
        tools=("fs_write", "fs_delete", "fs_move", "fs_mkdir"),
        predicate=lambda action, _ctx: any(
            is_forbidden_write(resolve_path(raw)) for raw in _all_paths(action)
        ),
    ),
    Policy(
        name="network_egress_not_allowed",
        verdict=Verdict.BLOCK,
        reason="Destination host is not on the approved network allowlist",
        tools=("browser_open", "browser_read", "browser_submit", "message_send", "email_send"),
        predicate=lambda action, ctx: bool(
            (host := _url_host(action)) and ctx.allowed_hosts and host not in ctx.allowed_hosts
        ),
    ),
    Policy(
        name="credential_change_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Changing credentials, keys or account security always needs the user",
        tools=("credential_change", "fs_write", "terminal_run"),
        predicate=lambda action, _ctx: bool(
            re.search(
                r"(?i)(\.env\b|id_rsa|keystore|\.ssh|2fa|recovery code)", _action_text(action)
            )
            or CREDENTIAL_SHAPE.search(_action_text(action))
        ),
        escalate_to=RiskLevel.CRITICAL,
    ),
    Policy(
        name="financial_action_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Financial actions require explicit human confirmation",
        tools=("purchase", "financial_transaction", "browser_submit", "browser_click"),
        predicate=lambda action, _ctx: bool(
            re.search(r"(?i)\b(purchase|buy|checkout|place order|pay\b|payment|transfer|invoice)\b", _action_text(action))
        ),
        escalate_to=RiskLevel.CRITICAL,
    ),
    Policy(
        name="outbound_message_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Sending messages, forms or email on the user's behalf needs confirmation",
        tools=("message_send", "email_send", "browser_submit"),
    ),
    Policy(
        name="delete_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Deletion is never automatic",
        tools=("fs_delete",),
    ),
    Policy(
        name="bulk_delete_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Deleting many targets at once needs a reviewed plan",
        tools=("fs_delete",),
        predicate=lambda action, _ctx: _count(action) > 5,
        escalate_to=RiskLevel.CRITICAL,
    ),
    Policy(
        name="package_install_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Installing software changes the machine environment",
        tools=("package_install", "terminal_run"),
        predicate=lambda action, _ctx: bool(
            re.search(r"(?i)\b(pip install|npm i\b|npm install|winget install|choco install|apt-get install)\b", _command(action))
        ),
    ),
    Policy(
        name="self_modification_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Dobot does not modify its own security configuration without the user",
        tools=("fs_write", "terminal_run", "skill_run"),
        predicate=lambda action, _ctx: bool(
            re.search(r"(?i)(dobot[/\\](app|config|security)|\.env\b|policies\.py|risk\.py)", _action_text(action))
        ),
    ),
    Policy(
        name="unknown_terminal_binary_requires_approval",
        verdict=Verdict.APPROVAL,
        reason="Command uses a binary outside the reviewed allowlist",
        tools=("terminal_run",),
        predicate=lambda action, _ctx: bool(
            (command := _command(action).strip())
            and (binary := command.split()[0].split("/")[-1].split("\\")[-1].lower())
            and binary not in SAFE_COMMANDS
        ),
    ),
]


class PolicyEngine:
    def __init__(self, policies: list[Policy] | None = None) -> None:
        self.policies = policies if policies is not None else list(BUILTIN_POLICIES)

    def evaluate(self, action: ActionSpec, ctx: PolicyContext) -> list[PolicyOutcome]:
        outcomes: list[PolicyOutcome] = []
        for policy in self.policies:
            if policy.matches(action, ctx):
                outcomes.append(
                    PolicyOutcome(
                        policy=policy.name,
                        verdict=policy.verdict,
                        reason=policy.reason,
                        escalate_to=policy.escalate_to,
                        grantable=policy.grantable,
                    )
                )
        if not outcomes:
            outcomes.append(
                PolicyOutcome(policy="default_allow", verdict=Verdict.ALLOW, reason="No policy matched")
            )
        return outcomes

    @staticmethod
    def combine(outcomes: list[PolicyOutcome]) -> Verdict:
        """BLOCK beats APPROVAL beats ALLOW."""
        if any(outcome.verdict is Verdict.BLOCK for outcome in outcomes):
            return Verdict.BLOCK
        if any(outcome.verdict is Verdict.APPROVAL for outcome in outcomes):
            return Verdict.APPROVAL
        return Verdict.ALLOW

    def summarise(self) -> list[dict[str, str]]:
        return [
            {"name": policy.name, "verdict": policy.verdict.value, "reason": policy.reason}
            for policy in self.policies
        ]
