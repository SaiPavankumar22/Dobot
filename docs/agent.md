# Dobot Agent Runtime

Dobot orchestrates an execution runtime rather than reimplementing computer use. The runtime sits
behind `app/agents/hermes.py`.

## Modes

| `AGENT_RUNTIME` | Behaviour |
| --- | --- |
| `local` (default) | Dobot's own guarded tool implementations. No external runtime, no install. |
| `cli` | Shell out to the Hermes CLI (`HERMES_COMMAND`, `HERMES_ARGS`) for a planned prompt. |
| `remote` | POST the plan to a Hermes / MCP HTTP endpoint (`HERMES_ENDPOINT`). |

`local` exists so the hackathon demo never depends on a third-party install being healthy; `cli` and
`remote` are how Dobot hands real work to Hermes in a deployment.

## Tool surface

| Tool | Risk floor | Notes |
| --- | --- | --- |
| `tavily_search` / `tavily_research` | LOW | Web research, source collection |
| `fs_read` / `fs_list` | LOW | Reads inside allowed paths |
| `fs_write` | MEDIUM | Create/overwrite; content hash recorded |
| `fs_move` | MEDIUM | Move/rename inside allowed paths |
| `fs_delete` | HIGH | Never runs without approval |
| `terminal_run` | MEDIUM→HIGH | Allowlisted binaries, hard timeout, cwd restricted |
| `browser_open` / `browser_read` | LOW/MEDIUM | Navigate + read content |
| `computer_*` | MEDIUM | Hermes computer use: click, type, scroll, drag |
| `screen_capture` / `screen_analyze` | LOW | On-demand capture, OCR + vision |
| `memory_write` | LOW | Explicit "remember this" |
| `app_open` | LOW | Launch an application |
| `skill_run` | inherited | Reusable workflows from `skills/` |

Every tool declares `name`, `description`, `risk_floor`, JSON parameter schema, and a `verify()`
implementation so the Verifier knows how to check the result.

## Hermes integration detail

Hermes exposes its toolsets (`computer_use`, `browser_exec`, `terminal`, `filesystem`, `skills`) and
speaks MCP over stdio to `cua-driver` for background desktop control. Dobot's adapter:

1. Builds the prompt from the approved plan (including the constraint list produced by the decision
   engine, so the runtime cannot exceed what the user approved).
2. Runs it with the configured toolset allowlist (`HERMES_ALLOWED_TOOLS`).
3. Streams stdout into `tool_completed` events and a hard timeout into `tool_failed`.
4. Hands the result back to the Verifier, which checks the *system state*, not the runtime's claim.

Approval grants are passed as explicit capability envelopes: if the user approved "move 54 files",
the envelope names those files, and anything outside it is re-evaluated by the decision engine rather
than silently allowed.

## Skills

Skills are directories under `skills/` containing `SKILL.md` (human-readable intent and safety
requirements) and optional `workflow.json` (machine-readable steps). `app/skills_loader.py` parses
both, exposes them to the planner as callable tools, and the desktop Skills page lists them.

Dobot can propose a new skill after observing a repeated pattern; the user confirms and it is written
to disk. Learned skills are reviewable and deletable — same principle as memory.

## Scheduled work

`app/core/scheduler.py` runs cron automations and one-shot reminders through APScheduler. A scheduled
task is a normal task: same context, same decision engine, same approvals, same verification. A
scheduled task that needs approval raises it and waits rather than bypassing the gate.
