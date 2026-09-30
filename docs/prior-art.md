# Prior art: what Dobot borrowed, and what it rejected on purpose

Four open-source projects were studied closely (September 2026) while pushing Dobot from
hackathon-ready to production-ready. This record states what was taken, where it landed in the
codebase, and what was deliberately left behind — a borrow without provenance is just a feature
nobody can explain.

## [elie222/rakazo](https://github.com/elie222/rakazo) — persistent AI teammates, bring-your-own computer

**What it is.** Open-source Grok Bot alternative (Apache-2.0): persistent bots with their own
conversations, memory, routines and history; web + Electron + Expo clients; PostgreSQL/Prisma,
Hono/oRPC, Graphile Worker; and *computer providers* — Docker, E2B, Daytona, CreateOS, Box — chosen
by a single `SANDBOX_PROVIDER` variable.

**Borrowed:**

- **A sandbox is a provider, not a feature.** Rakazo swaps the whole computer behind one env var.
  Dobot already had that shape for NemoClaw (`build_sandbox()` in `app/agents/sandbox.py`); rakazo
  is what justified adding a *third* backend — `nebius` — instead of a one-off code path.
- **Persistent computers.** A bot's computer keeps its own filesystem state between commands.
  Dobot's Nebius provider does the same: every `terminal_run` chains onto the previous ConTree
  checkpoint, so files written in one command survive into the next.
- **The UI must state which provider is live.** Rakazo's docs never hide which computer a bot is
  on; Dobot's Security page already carried that contract, and the nebius status reuses it
  (`provider · isolation · degraded`, and `isolation: none` plus a reason when unreachable).
- **Local-first degradation.** Rakazo's whole install story is "works locally, upgrades optional".
  That mirrors Dobot's design rule #1 (one interface per dependency, each with a working local
  fallback) — useful confirmation, not new code.

**Rejected on purpose:** PostgreSQL/Prisma (Dobot's store interface keeps the local-file default
and MongoDB/Zilliz as optional extras), multi-user accounts and auth (single-person laptop tool),
the Composio/Pipedream integration catalogs, and the mobile client.

## [milind-soni/OpenMausBot](https://github.com/milind-soni/OpenMausBot) — a roster of bots, each with a computer

**What it is.** Open-source Grok Bot alternative (Apache-2.0) shaped like a messaging app: every
sidebar entry is a real agent (claude/codex/grok CLIs) with its own personality, model, cloud
computer and connected apps. A harness server on `127.0.0.1` owns every agent process and
normalizes each provider's protocol into one event stream; a permission broker turns every risky
action into an inline Allow/Deny decision.

**Borrowed:**

- **A permission broker.** Shell commands, file edits and questions surface as cards the human
  answers. Dobot's decision engine + ApprovalCard + permission grants (*Allow once* / *Always
  allow* / *Reject*, revocable under Security → Granted permissions) is the same pattern with a
  deterministic policy layer underneath — the model never gets to approve itself.
- **Every bot gets a computer.** The single biggest borrow: Dobot's `nebius` sandbox provider
  gives its agent a persistent, VM-isolated computer in the cloud, with no local daemon —
  the answer to the long-standing "OS-level sandbox: not on Windows" gap.
- **Degrade, never crash.** OpenMausBot's drivers "degrade to *unavailable*, never crash the
  fleet". `FallbackSandbox` now behaves the same way for both remote providers: configured but
  unreachable becomes a degraded status with a reason, not an exception storm — and a failed
  command still runs through the local action firewall instead of dying.
- **A bounded control surface.** OpenMausBot ships an MCP server that exposes tasks and reads but
  explicitly *not* approvals, deletion, credentials or computer lifecycle. That is the exact
  boundary Dobot would keep if it exposes its own external control surface later.

**Rejected on purpose:** bring-your-own-CLI engines (Dobot owns its runtime and model tiers),
channels/roster UX, the Composio app marketplace, and voice-call flows (Dobot's voice work is
local-first: OS speech + Whisper).

## [eigent-ai/eigent](https://github.com/eigent-ai/eigent) — a coworking desktop of agents

**What it is.** CAMEL-based multi-agent cowork desktop: FastAPI/uv backend, React/Electron shell,
CI audit workflows, heavy automation tooling.

**Borrowed:** the habit of shipping *operations evidence* with features — its CI audit workflows
inspired `skills/ci_autopsy` (turn a failing pipeline into a diagnosed, cited report), and the
automation-first posture reinforced the skills/workflow direction Dobot already had. Its structure
(backend API + desktop shell + optional services) also served as a sanity check that Dobot's
layout matches how comparable tools are actually built.

## [razzant/ouroboros](https://github.com/razzant/ouroboros) — a self-creating agent

**What it is.** An agent that builds and maintains itself, with release evidence and durable
identity/memory as first-class concerns.

**Borrowed:** evidence-backed releases → `skills/release_checklist`; the emphasis on *durable*
memory validated Dobot's consolidation/canonical-facts pipeline rather than changing it; and the
pair `skills/action_items` / `skills/disk_report` came out of the same pass — small, reusable
workflows the planner can adopt without inventing them each run.

## Where this landed, in one table

| Borrow | Source | Landed in |
| --- | --- | --- |
| Third sandbox provider (`nebius`) behind the existing interface | rakazo's provider model | `app/agents/sandbox.py`, `SANDBOX_PROVIDER` validator in `config.py` |
| Persistent, versioned remote session per agent | rakazo computers + OpenMausBot computer-per-bot | `NebiusSandbox._execute` (chained checkpoints, `disposable=False`) |
| Honest degraded status, degrade-don't-crash | OpenMausBot driver SPI | `FallbackSandbox`, `NebiusSandbox.status`, Security page |
| Allow-once / always-allow permission grants | OpenMausBot permission broker | `app/security/grants.py`, ApprovalCard, Security → Granted permissions |
| CI audit as a reusable workflow | eigent | `skills/ci_autopsy` |
| Release evidence as a skill | ouroboros | `skills/release_checklist` |

Related: the earlier production-readiness pass added `skills/action_items`, `skills/disk_report`,
the shape-based credential matching in `app/security/risk.py`, and CI that installs the `dev`
extra so the suite actually runs on every push.
