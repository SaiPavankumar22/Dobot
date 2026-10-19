# Dobot Agent Runtime

Dobot orchestrates an execution runtime rather than reimplementing computer use. The runtime sits
behind `app/agents/hermes.py`.

## Modes

| `AGENT_RUNTIME` | Behaviour |
| --- | --- |
| `local` (default) | Dobot's own guarded tool implementations. No external runtime, no install. |
| `cli` | Shell out to the Hermes CLI (`HERMES_COMMAND`, `HERMES_ARGS`) for a planned prompt. |
| `remote` / `api` / `hermes` | Drive the real Hermes gateway over HTTP: `POST /v1/runs`, then the SSE event stream (`HERMES_ENDPOINT`, `HERMES_API_KEY`). |
| `auto` | The API runtime, probed on a TTL first — the sensible default for a containerized deployment. |

With `HERMES_FALLBACK_LOCAL=true` (default) the HTTP modes are wrapped in `ResilientRuntime`:
gateway reachable → desktop delegation (`hermes_run`, `computer_*`) works; unreachable → those
tools fail with the reason, the planner drops them instead of planning gestures that cannot run,
and everything else (files, terminal, browser, research, memory) keeps working. Set it `false`
for a strict "Hermes or nothing" deployment.

`local` exists so Dobot never depends on a third-party install being healthy; `remote` is how Dobot
hands real work — including desktop control — to the upstream Hermes agent. The HF Space runs
**both servers in one container** (gateway sidecar on loopback beside the backend) — see
[`docs/hermes.md`](hermes.md) §4 for that topology and the tunnel alternative for a real desktop.
See [`docs/hermes.md`](hermes.md) for the wire contract, setup and verification evidence.

## Tool surface

| Tool | Risk floor | Notes |
| --- | --- | --- |
| `tavily_search` / `tavily_research` | LOW | Web research, source collection |
| `fs_read` / `fs_list` | LOW | Reads inside allowed paths |
| `fs_write` | MEDIUM | Create/overwrite; content hash recorded |
| `fs_move` | MEDIUM | Move/rename inside allowed paths |
| `fs_delete` | HIGH | Never runs without approval |
| `terminal_run` | MEDIUM→HIGH | Allowlisted binaries, hard timeout, cwd restricted |
| `browser_open` / `browser_read` | LOW | Fetch page text over HTTP (read-only, no window) |
| `browser_navigate` / `browser_snapshot` | LOW | Open the visible browser, index interactive elements (role, accessible name, `disabled`/`obscured` marks) |
| `browser_click` / `browser_type` | MEDIUM | Drive the visible browser (local Playwright driver: identity-pinned handles, wait-and-retry, post-action `evidence`) |
| `computer_*` | MEDIUM | Single gesture via Hermes computer use: click, type, scroll, drag |
| `hermes_run` | MEDIUM | Whole desktop job delegated to the Hermes agent (multi-step GUI work) |
| `screen_capture` / `screen_analyze` | LOW | On-demand capture, OCR + vision |
| `memory_write` | LOW | Explicit "remember this" |
| `app_open` | LOW | Launch an application |
| `skill_run` | inherited | Reusable workflows from `skills/` |

Every tool declares `name`, `description`, `risk_floor`, JSON parameter schema, and a `verify()`
implementation so the Verifier knows how to check the result.

## Hermes integration detail

Hermes exposes toolsets (`computer_use`, `browser`, `terminal`, `file`, `web`, `skills`, …) and
speaks MCP over stdio to `cua-driver` for background desktop control. Dobot's adapter:

1. Builds the prompt from the approved plan (including the constraint list produced by the decision
   engine, so the runtime cannot exceed what the user approved).
2. Creates a run (`POST /v1/runs`) and consumes its SSE stream, keeping the `tool.started` /
   `tool.completed` trace as evidence.
3. Turns a timeout into a server-side `/stop`, and a failed run into a typed error — a run is never
   reported as success because the HTTP call returned.
4. Hands the result back to the Verifier, which checks the *system state*, not the runtime's claim.

Earlier versions of this adapter called `/run`, `/computer` and `/browser/submit`. Those routes never
existed upstream, so `remote` mode could not work; the real contract is in [`hermes.md`](hermes.md).

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
