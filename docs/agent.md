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
| `skill_view` | LOW | Read a skill's complete instructions, plus optionally one file from that skill's own folder |
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

There are **38**: ten written for Dobot (*tidy downloads*, *weekly report*, *CI autopsy*, *release
checklist*, …) and twenty-eight imported from
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) under MIT —
*systematic-debugging*, *codebase-inspection*, *github*, *pdf*, *microsoft-office*, *arxiv*,
*obsidian*, *meeting-action-items* and the rest. An import is rewritten into Dobot's frontmatter,
gets a short adaptation header mapping Hermes' tool names (`terminal`, `file`, `browser`, `web`, …)
onto ours, and must clear `security/skill_scanner.py` before it can be offered: one skill
(`blocked-page-recovery`) and nine of its support files were refused by that gate and left out.
Skills that need another platform or another agent's runtime — the Apple suite, `computer-use`,
`claude-code`, the credential-bound Google/Microsoft/X ones — are not imported at all.

Reading is progressive, as it is in Hermes: the planner sees one line per skill, `skill_view(name)`
pulls the whole document (long skills run past 14 KB, well past `skill_run`'s 4 KB preview), and
`skill_run(name)` executes it. `skill_view` may also open a single file inside that skill's folder
and refuses anything outside it.

Dobot can propose a new skill after observing a repeated pattern; the user confirms and it is written
to disk. Learned skills are reviewable and deletable — same principle as memory.

## Scheduled work

`app/core/scheduler.py` runs automations and one-shot reminders through APScheduler. Schedules are
read by `app/core/schedules.py`, which accepts a five-field cron expression, a natural phrase the
user said out loud (`every monday 9am`, `weekdays at 9am`, `every day at 8pm`), a recurring interval
(`every 30m`, `every 2h`) or a one-shot (`in 30m`, `tomorrow at 8pm`, an ISO timestamp) — the
natural-language parsing and catch-up rule adapted from Hermes' `cron/jobs.py`. A one-shot is
persisted as the instant it resolved to, so a restart cannot shift it.

On startup, a run missed while Dobot was off fires **once** if it is still fresh: inside half a
period clamped to two minutes – two hours (two hours for a reminder). Anything older is logged and
skipped rather than fired late, and accumulated misses never replay.

A scheduled task is a normal task: same context, same decision engine, same approvals, same
verification. A scheduled task that needs approval raises it and waits rather than bypassing the
gate. When one finishes while the user is elsewhere, the shell raises a system notification titled
*Dobot reminder* for a reminder and *Dobot scheduled task* for a recurring job.

### What came from Hermes' cron, and what did not

The engine was not imported — `cron/scheduler.py` alone is ~4,400 lines wired into Hermes' gateway,
profiles and ledgers, and Dobot already had APScheduler (see [`prior-art.md`](prior-art.md)). The
grammar and the missed-run policy were; the rest of Hermes' cron surface is honestly absent:

| Hermes capability | In Dobot |
| --- | --- |
| Cron / interval / natural / one-shot schedule grammar | ✅ `app/core/schedules.py` (ported, tested) |
| Missed-run catch-up with a grace window | ✅ `Scheduler._missed_run` — half a period, clamped 2 min – 2 h |
| Create / edit / pause / resume / run-now / delete | ✅ `/automations` endpoints, `set_status`, `run_now` |
| Durability across restart | ✅ `RecordStore("automations")` + `_restore` |
| Overlap guard so a job never runs twice at once | ✅ `Scheduler._running` |
| Runs go through the normal approvals and verification | ✅ by design |
| Completion notification | ✅ event bus → in-app toast + system notification |
| Per-job IANA timezone | ❌ machine timezone only |
| Per-job `grace_minutes` override (incl. `0` = never catch up) | ❌ the policy is fixed |
| Retry of transient provider failures (5 / 15 / 30 min) | ❌ a failed run is logged and announced |
| Failure-incident suppression (same signature, 6 h) | ❌ every failed run announces itself |
| Execution ledger of attempts (`unknown`, timeout, ownership) | ❌ only `run_count` and `last_run_at` |
| Durable delivery queue (replay after a restart) | ❌ events are emitted live; nothing replays them |
| Script-only jobs (`no_agent`) | ❌ every job runs through the agent |
| Per-job skills, model or toolset pinning | ❌ jobs use the planner's default configuration |
| Global concurrency cap (`max_parallel_jobs`) | ❌ only the per-job overlap guard |
| Silent results (`[SILENT]`) | ❌ every completion announces |
| Suggestion / blueprint catalog | ❌ |
