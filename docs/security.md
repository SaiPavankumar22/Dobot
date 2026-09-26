# Dobot Security Model

Dobot performs real actions on a real computer. Security is a product feature, not a footnote.

## Layers

```
1. Gateway        FastAPI binds 127.0.0.1 by default; no credential ever crosses to the UI
2. Context        screen capture only on demand; OCR text is ephemeral
3. Decision       deterministic policies authoritative, JEV advisory, risk classification
4. Approval       human gate for HIGH and CRITICAL actions
5. Sandbox        NemoClaw / OpenShell: filesystem policy, network policy, credential custody
6. Verification   execution results checked before Dobot claims success
7. Kill switch    global cancellation of the active task and its pending tool calls
```

## Risk levels

| Level | Examples | Behaviour |
| --- | --- | --- |
| `LOW` | read webpage, search web, read file, inspect screen, summarize | execute automatically |
| `MEDIUM` | create file/folder, modify non-critical file, navigate website, create draft | execute, logged, reversible-preferred |
| `HIGH` | delete file, modify important file, send message, submit form, run destructive command | require approval |
| `CRITICAL` | purchase, financial transaction, credential change, permanent deletion, account change | always require explicit human confirmation, never auto-run |

Classification is deterministic first (`app/security/policies.py`): matched by tool, verb, and target
pattern. JEV can only *escalate* risk, never lower it below the policy floor — that invariant is
enforced in code and covered by tests.

### Stricter than the default: `REQUIRE_WRITE_APPROVAL`

Some people are not comfortable with *any* unattended change to their files. Setting
`REQUIRE_WRITE_APPROVAL=true` adds one deterministic policy, `writes_require_approval`, which turns
every mutating tool into an approval gate even when the risk is only `MEDIUM`:

| Tool family | Default | With `REQUIRE_WRITE_APPROVAL=true` |
| --- | --- | --- |
| `fs_write`, `fs_mkdir`, `fs_move`, `fs_delete` | `MEDIUM` → ALLOW (delete is already APPROVAL) | APPROVAL |
| `terminal_run`, `package_install` | `MEDIUM` → ALLOW | APPROVAL |
| `skill_run` | `MEDIUM` → ALLOW | APPROVAL |
| `computer_click` / `_type` / `_key` / `_drag` | `MEDIUM` → ALLOW | APPROVAL |
| `fs_list`, `fs_read`, `tavily_*`, `memory_write`, `screen_*` | ALLOW | **unchanged — ALLOW** |

Reads stay automatic on purpose: the setting gates *changes*, not inspection, otherwise ordinary
questions about your own files would turn into approval queues. `skill_run` is in the list
specifically because an unexpanded skill executes its whole workflow under a single decision, so
leaving it out would let a skill change files without ever being gated.

The **Security** page shows whether this is on, and it is reported in `GET /security/status` as
`require_write_approval`, so the UI and the backend can never disagree about the posture.

**One honest consequence, observed live.** `terminal_run` is classified as a mutating tool, because a
shell command can write. So with this on, *every* terminal command asks — including a read-only one
like `git status`. Dobot cannot prove a shell command is read-only without parsing it, and guessing
wrong in the permissive direction is the more expensive mistake. If the friction matters, use a mode
(`assist`) for the same effect on files while leaving read-only shell commands alone, or narrow it by
removing `terminal_run` from `MUTATING_TOOLS` in `app/security/policies.py` and accepting that a
shell one-liner can then move a file unattended.

## Execution modes

The mode you pick per message *is* the approval threshold (`MODE_APPROVAL_THRESHOLD` in
`app/core/decision_engine.py`). Above the threshold a human decides; below it, Dobot acts.

| Mode | Threshold | Behaviour |
| --- | --- | --- |
| **Ask** | — | Answers and shows the plan. Executes nothing, by construction: it is expressed as shadow mode internally, so there is exactly one code path that cannot act. |
| **Assist** | `MEDIUM` | Anything beyond a read is confirmed first. |
| **Agent** *(default)* | `HIGH` | Safe, reversible work proceeds; `HIGH` and `CRITICAL` stop for you. This is the specification's default and is unchanged from V1. |

A mode is a ceiling, so a stricter global setting can collapse two modes into the same behaviour:
with `REQUIRE_WRITE_APPROVAL=true`, **Assist and Agent behave identically for file changes**, because
the floor is already stricter than both. That is the intended monotonicity, not a bug — but if you
want the three modes to be visibly distinct, drive the strictness from the mode and leave
`REQUIRE_WRITE_APPROVAL=false`.

The ladder is monotonic. Policies, JEV and `REQUIRE_WRITE_APPROVAL` can only make a decision
*stricter* than the mode allows, never looser — a mode is a ceiling on autonomy, not a bypass. The
mode is persisted on the resumable task state, so a plan paused at an approval gate keeps the
threshold it was planned under rather than silently relaxing to the default on resume.

### File guard

`PROTECTED_PATHS` is an **independent** guard, deliberately not part of the command guards and not
derived from `SANDBOX_ALLOWED_PATHS` — which defaults to your whole home directory, and therefore
protects nothing of real value. The default list covers credential stores (`~/.ssh`, `~/.aws`,
`~/.gnupg`, `~/.kube`, `~/.netrc`, `~/.config/gh`, `~/.config/gcloud`, `~/.docker/config.json`).

It **blocks reads as well as writes**. Reading a private key is a credential disclosure even though it
changes nothing on disk, and a guard that only watched writes would let `cat ~/.ssh/id_rsa` straight
through. It also matches two shapes:

- a tool that carries a real path (`fs_read` on `~/.ssh/id_rsa`), and
- a shell command that merely *mentions* one (`cat ~/.ssh/id_rsa`, `type .env`, `copy id_rsa …`).

Both spellings of every protected root are compared, because commands are written the way a person
writes them (`~/...`) while the roots are resolved absolute paths — matching only the absolute form
silently misses the common case.

### Shell evasion

A literal list of dangerous verbs is trivial to walk around, so the command guards look for the
*shape* of an evasion attempt instead. Two tiers, because intent differs:

- **BLOCK** — no honest use in an assistant: a payload piped into a shell (`… | sh`, `-EncodedCommand`,
  `FromBase64String`), fetch-and-execute (`curl … | sh`, `iwr … | iex`), reverse shells (`nc -e`,
  `/dev/tcp/`, `socat … exec:`), decoding with a Windows tool (`certutil -decode`), audit tampering
  (`history -c`, `wevtutil cl`), persistence (`schtasks /create`, `reg add …\Run`, `crontab -`), and
  transmitting a private key.
- **APPROVAL** (escalated to `HIGH`) — obfuscation and oddity with a possible innocent reading:
  `eval(…)`, `$IFS` word-splitting, hex escapes, piping an encoded blob, `chmod +x && ./…`, an inline
  socket script.

Reviewed commands (`dir`, `git status`, `python -m pytest`, `npm run build`) are untouched — the rules
are pattern-shaped, not verb-shaped, precisely so that ordinary work is not interrupted.

### Skill scanner

A skill is not a tool. A tool has a fixed signature and a declared risk floor; a skill is *prose and a
workflow* that the model reads as guidance. That makes `skills/` a real attack surface, and every
collection of third-party skills says the same thing: they are unaudited, review them first.

Every skill is scanned at load time by `app/security/skill_scanner.py` and gets a status:

| Status | Meaning |
| --- | --- |
| `clean` | nothing matched |
| `allowlisted` | named in `SKILL_SCAN_ALLOWLIST`, trusted regardless |
| `warn` | findings, but not refused (`SKILL_SCAN_MODE=warn`) |
| `blocked` | a critical finding and `SKILL_SCAN_MODE=block` (the default) |
| `off` | scanning disabled |

Rules, by severity:

- **critical → blocked**: prompt injection ("ignore all previous instructions", "do not tell the
  user", "without asking", "bypass the approval"), hardcoded credentials (`sk-…`, `ghp_…`, `AKIA…`,
  private-key headers, JWTs, `api_key = "…"`), exfiltration (chat webhooks, uploads of a file or
  credential, `base64 | curl`), and dangerous or evasive commands.
- **medium → warn**: a step naming a tool the registry does not declare (so it cannot be
  risk-classified), an unparseable `workflow.json` (high), a hardcoded absolute path.

Enforcement is twofold, because reporting alone is not a control:

1. a blocked skill is **excluded from everything the planner sees** — `matching()`, `prompt_section()`
   and `active()` — so it can never be proposed; and
2. a `skill_run` naming a blocked skill is **BLOCKed** by the `blocked_skill_refused` policy, so it
   cannot be run directly either.

`records()` still returns it, because the UI must be able to show you what was refused and why.
Skills Dobot writes itself are scanned like any other: `save()` does not exempt its own output.

> One source of truth matters here. The scanner's first version restated the dangerous-command
> patterns by hand and used a bare `\bformat\b`, which matched the word "format" in ordinary prose and
disabled Dobot's own `weekly_report` skill. It now imports the policy layer's patterns directly, and
> a test asserts every shipped skill survives the scanner.

## Posture, in one place

`GET /security/permissions` returns the access policy at **tool-level granularity** — for every
capability: its declared risk floor, whether that floor runs unattended in the active mode, whether a
condition can make it ask, whether one can make it refuse, and which guards apply. `GET
/security/skills` returns the scan reports. Both are rendered on the Security page, so the posture you
read is computed from the same objects the decision engine actually evaluates.

## Policy engine

Policies are data, not prose:

```python
Policy(name="no_credentials_in_shell",
       match=Pattern(tool="terminal", command_regex=r"(?i)(password|api[_-]?key|secret)"),
       verdict=Verdict.BLOCK,
       reason="Refusing to pass credentials through a shell command")
```

Verdicts: `ALLOW`, `APPROVAL`, `BLOCK`. A single `BLOCK` in the plan short-circuits the whole task.
`SHADOW_MODE=true` downgrades every verdict to plan-only, so nothing executes.

## Sandbox contract

`app/agents/sandbox.py` is the only path from a tool to the OS when a sandbox provider is configured:

- **filesystem** — every path is resolved and must fall inside `SANDBOX_ALLOWED_PATHS`
- **network** — every outbound host must match `SANDBOX_ALLOWED_NETWORK`
- **process** — commands are checked against an allowlist and run with a hard timeout
- **credentials** — the API keys live in the backend process; sandboxed execution receives
  placeholders, and NemoClaw substitutes real values at the approved egress boundary

With `SANDBOX_PROVIDER=local` the same checks run inside Dobot (the action firewall) but there is no
kernel-level isolation. The dashboard's Security page states this explicitly rather than implying
safety it does not have.

## Screen privacy

- Continuous monitoring is off and cannot be turned on implicitly. Captures are triggered only by an
  explicit region selection, an approved workflow, or a screen-aware automation the user enabled.
- The selection overlay is a temporary transparent window; it closes immediately after capture.
- Selected pixels are not written to disk or memory. Only text the user asked Dobot to remember is
  persisted, and the Memory page allows deletion.

## Human control

- The dot shows a distinct "controlling your computer" state while an execution is running, with an
  inline STOP control.
- Global kill switch (default `Ctrl+Shift+Esc`) cancels the active task, terminates pending tool
  calls, and stops automation. Every subsequent tool call sees the cancellation token.
- Nothing is hidden: the activity timeline records every decision, tool call, verification, and
  failure.

## Secrets

`.env` is gitignored. The desktop app receives only connection status
(`connected` / `not_connected` / `requires_authentication`) from `GET /settings/providers`, never a
key. Logs redact anything matching the configured secret values.
