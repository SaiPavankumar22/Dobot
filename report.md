# Dobot — Development & Verification Report

This is the engineering record: what was built, what changed between versions, **how each claim was
tested**, what broke, and what is still unproven. It is written so that someone who was not in the
room can tell a verified claim from a hopeful one.

Read §6 for the testing method, §8 for the mistakes that were made (including one that touched real
user data), and §9 for the honest list of what has *not* been verified.

---

## 1. Version map

| Version | Label | What it is | Test count |
| --- | --- | --- | --- |
| **V1** | MVP `0.1.0` | The full MVP from the specification: backend, decision engine, memory, tools, dot-first desktop shell. Built before this report. | 102 |
| **V2** | Chat-first `0.1.0` | Turns the dot-first shell into a chat-first application with the dot as an opt-in, makes the product installable, adds strict write confirmation, and fixes four real defects. | 106 |
| **V2.5** | Hardened `0.1.0` | A study of three reference projects (Leon, QwenPaw, a 50-use-case OpenClaw collection) turned into four capabilities Dobot was missing: execution modes, a file guard, shell-evasion detection, and a skill scanner. Found two further defects. | 137 |
| **V3** | *proposed* | Not built. See §13. | — |

> **Version strings are still `0.1.0`** in `backend/app/__init__.py`, `desktop/package.json`,
> `desktop/src-tauri/Cargo.toml` and `desktop/src-tauri/tauri.conf.json`. V2 is a behavioural version,
> not a declared one: the shipped installers are named `Dobot_0.1.0_*`. Bumping to `0.2.0` is a
> four-file change plus a rebuild — deliberately not done here, because a source tree that says
> `0.2.0` next to `0.1.0` artefacts is a worse inconsistency than a stale number.

---

## 2. V1 — what existed before this session

The specification's phases 1–11, implemented and verified against a live environment.

**Backend** (`backend/`, FastAPI + asyncio)

| Area | Modules |
| --- | --- |
| Gateway | `app/main.py` (lifespan, health, providers), `app/api/{chat,screen,research,tasks,automations,approvals,memory,skills,settings,security,activity,ws}.py` |
| Core loop | `app/core/orchestrator.py`, `context_engine.py`, `planner.py`, `router.py`, `decision_engine.py`, `verifier.py`, `scheduler.py`, `activity.py`, `killswitch.py` |
| Reasoning | `app/agents/nemotron.py` (Nebius), `hermes.py` (3 runtimes), `sandbox.py` (NemoClaw/OpenShell), `research_agent.py` |
| Safety | `app/security/risk.py`, `policies.py`, `jev.py`, `approvals.py` |
| Memory | `app/memory/{manager,retriever,store,vectors,embeddings}.py` |
| Tools | `app/tools/{base,tavily,filesystem,terminal,browser,screen,computer,apps,productivity,registry}.py` |

**Desktop** (`desktop/`, Tauri 2 + React 18 + TypeScript + zustand)

Dot-first: the transparent always-on-top `dot` window was visible on launch, with a `panel` window
anchored to it, a `dashboard` window, and a temporary `overlay` for region selection. Global hotkeys,
tray, autostart, native screen capture via `xcap`.

**Also present:** `skills/{clean_downloads,research_topic,weekly_report}/`, `docs/{architecture,security,memory,agent,development}.md`,
`examples/`, `LICENSE` (Apache 2.0), `.env.example`, `.gitignore`.

### V1 hardening pass (earlier session, recorded here for completeness)

Seven functional bugs were found by running the loop for real and were fixed — do not re-introduce
them:

| # | Defect | Root cause |
| --- | --- | --- |
| 1 | Every write escalated to `HIGH` | `ActionSpec.reversible` defaulted to `False`; changed to `bool \| None` |
| 2 | Path policy only checked the first path argument | added `_all_paths()` covering `paths`/`sources` |
| 3 | An approved step re-asked for approval forever | missing `ResumableState.granted` |
| 4 | `ActivityLog` / `MemoryManager.prune` awaited a sync predicate | async-predicate misuse |
| 5 | WebSocket stuck in `CONNECTING`, handlers double-subscribed | React StrictMode; added a module `subscribed` guard |
| 6 | The immediate `response.answer` never reached the placeholder bubble | `send()` patched the wrong message |
| 7 | Background tasks ending at a gate never refreshed their bubble | `finaliseTask` not called on `approval_required` / `shadow_plan` |

Also fixed in that pass: `TaskVerification` → pydantic `BaseModel`; `_IRREPLACEABLE` needed
`re.IGNORECASE` rather than an inline `(?i)` mid-pattern; `selftest.py` forces UTF-8 stdout;
`/health` reports `unreachable` when a key exists but the endpoint fails; the WS `hello` payload now
carries `dot_status` + `active_tasks` so a reconnecting client cannot render stale state; and
`tsconfig.node.json` needed `emitDeclarationOnly` + `declaration`.

**Two configuration defects fixed in V1 that are easy to hit again:**

- `NEBIUS_BASE_URL` must be `https://api.tokenfactory.nebius.com/v1`. The legacy
  `https://api.studio.nebius.com/v1` returns **404** on `/chat/completions`.
- Model ids were replaced with ones actually served, verified against `GET /v1/models`:
  `nvidia/Nemotron-3-Ultra-550b-a55b`, `nvidia/nemotron-3-super-120b-a12b`,
  `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, `openbmb/MiniCPM-V-4_5`, `Qwen/Qwen3-Embedding-8B`.

---

## 3. V2 — what changed in this session

### 3.1 Started from a broken tree

The session opened on a **half-applied refactor that did not compile**. `state.rs` had been rewritten
to a `dot_enabled` model (dropping `dot_visible` and `set_dot_visible`, and adding `scale` to
`DotPosition`), and one edit to `windows.rs` had landed calling `open_chat` — which did not exist.
Nothing else had been updated to match.

Consequence: `windows.rs::open_chat` undefined; `windows.rs` still called the deleted
`state::set_dot_visible`; `lib.rs` built `DotPosition` without `scale`; `lib.rs` still had
`toggle_dot`/`set_dot_visible` commands; `tauri.conf.json` had no `chat` window. **`cargo check`
could not pass.** Finishing this refactor was therefore prerequisite to everything else, not optional
polish.

### 3.2 Desktop shell — two modes

| Change | File |
| --- | --- |
| `CHAT` const, `open_chat`, `set_dot_enabled`, `toggle_dot`, `apply_startup_state`, `anchor_bottom_right`; removed `set_dot_visible` | `desktop/src-tauri/src/windows.rs` (rewritten) |
| `DotPosition.scale`; `DobotState.dot_enabled` defaulting to **false**; `is_dot_enabled` / `set_dot_enabled` | `desktop/src-tauri/src/state.rs` |
| `open_chat` / `dot_enabled` / `set_dot_enabled` commands; `apply_startup_state` in `setup`; `scale: 0.0` in the move handler; closing chat **quits** when the dot is off and **hides** when it is on | `desktop/src-tauri/src/lib.rs` |
| Tray: "Open Dobot" now opens chat, added "Quick ask panel", "Show / hide the dot" → "Always-on dot"; tray left-click opens chat | `desktop/src-tauri/src/tray.rs` |
| Added the `chat` window (1080×740, decorated, `visible: true`); set the `dot` window `visible: false` | `desktop/src-tauri/tauri.conf.json` |
| `chat` added to the capability window list | `desktop/src-tauri/capabilities/default.json` |

The always-on choice and the dot's position persist in
`%APPDATA%\com.dobot.desktop\dobot-state.json`. Every surface that can flip the dot (chat header,
panel, Security, Settings, tray) calls the same `set_dot_enabled` command, and the shell broadcasts
`dobot://dot-enabled` so the switches cannot disagree.

### 3.3 Desktop UI — the chat window

| Change | File |
| --- | --- |
| **New**: full chatbot page — conversation sidebar, thread with user/Dobot bubbles, per-answer status + model tier, plans, sources, pinned approvals, composer, welcome hero with four suggestion cards | `desktop/src/pages/Chat.tsx` |
| **New**: accessible toggle switch used for every always-on control | `desktop/src/components/Switch.tsx` |
| `chat` window kind; browser default is now `chat`; hash navigation between chat and dashboard | `desktop/src/App.tsx` |
| `dotEnabled`, `setDotEnabled`, `refreshDotEnabled`, `newConversation`, `mergeStepStatuses()`; removed `setDotVisible` | `desktop/src/store/dobotStore.ts` |
| `openChat`, `dotEnabled`, `setDotEnabled`, `onDotEnabled`; removed `toggleDot` / `setDotVisible` | `desktop/src/services/native.ts` |
| Toast gained a `placement` prop | `desktop/src/components/Toast.tsx` |
| Always-on switch + "write confirmation" panel | `desktop/src/pages/{Security,Settings}.tsx` |
| Chat layout, switch, hero, composer, toast placement, and the `min-height: 0` scroll fix | `desktop/src/styles.css` |
| `TaskStepRecord.verification` widened to the real verification shape | `desktop/src/types.ts` |

### 3.4 Backend

| Change | File | Why |
| --- | --- | --- |
| `step_records()` helper; step statuses now persisted when a task pauses at an approval gate, is blocked, or produces a shadow plan | `backend/app/core/orchestrator.py` | A paused task reported `steps: []`, so the UI could only show a stale all-pending plan |
| `Settings.require_write_approval` (env `REQUIRE_WRITE_APPROVAL`, default `false`) | `backend/app/config.py` | Opt-in strict confirmation |
| `PolicyContext.require_write_approval` + `MUTATING_TOOLS` + the `writes_require_approval` builtin policy | `backend/app/security/policies.py` | The actual gate |
| Pass the setting into the policy context | `backend/app/core/decision_engine.py` | Otherwise the setting would be inert |
| `require_write_approval` and the extended `require_confirmation_for` in `/security/status` | `backend/app/services.py` | UI and backend must not disagree about the posture |
| Pin `REQUIRE_WRITE_APPROVAL=false` in the test fixture | `backend/tests/conftest.py` | The developer's `.env` was leaking into assertions — see §8.2 |

### 3.5 Documentation and packaging

| Change | File |
| --- | --- |
| Full rewrite: vision, two modes, architecture, why-each-technology, real model ids, requirements, quickstart, hotkeys, API surface, security posture, repo layout, docs index, and an explicit gap table | `README.md` |
| **New**: build, install, backend-hosting options, autostart, data locations, updating, uninstalling, optional upgrades, troubleshooting, laptop security notes | `docs/install.md` |
| **New**: one-double-click launcher (backend + app) | `scripts/start-dobot.bat` |
| Window table, the always-on state machine, install pointer | `docs/development.md` |
| Lifecycle diagram now starts at the chat composer; five-window note | `docs/architecture.md` |
| The `REQUIRE_WRITE_APPROVAL` section with its tool-by-tool table | `docs/security.md` |
| **New**: this report | `report.md` |

---

## 4. The packageable build

The strongest single piece of evidence in V2 is that the app really does become an installer.

```
$ cd desktop && npx tauri build --debug
    Finished 2 bundles at:
        ...\target\debug\bundle\msi\Dobot_0.1.0_x64_en-US.msi
        ...\target\debug\bundle\nsis\Dobot_0.1.0_x64-setup.exe
```

| Artefact | Size |
| --- | --- |
| `Dobot_0.1.0_x64-setup.exe` (NSIS) | 3,128,039 bytes (3.1 MB) |
| `Dobot_0.1.0_x64_en-US.msi` | 5,255,168 bytes (5.1 MB) |
| `dobot-desktop.exe` (raw debug binary) | 16,676,864 bytes (16.7 MB) |

The bundler fetched WiX and NSIS on first use (`wix311`, `nsis-3.11`, `nsis_tauri_utils-v0.5.3`),
validated their hashes, then ran `candle`/`light` for the MSI and `makensis` for the NSIS installer.
This is documented in `docs/install.md` with the exact paths and sizes so nobody has to discover it.

**Not done:** a `--release` build. `[profile.release]` uses `lto = true, codegen-units = 1,
opt-level = "s"`, so the first release build is long; `--debug` was chosen to verify the *pipeline*
quickly. The pipeline is therefore proven, and the release profile is not. Also not done: installing
the bundle and running it natively (see §9).

---

## 5. Bugs found in V2

Five defects, each with the evidence that found it and the guard that prevents it now.

### B1 — The tree did not compile

**Found by:** reading the Rust sources before running anything, then `cargo check`.
**Cause:** a half-applied refactor (§3.1).
**Fix:** completed the `dot_enabled` refactor across `windows.rs`, `lib.rs`, `tray.rs`,
`tauri.conf.json`, `capabilities/default.json`.
**Guard:** `cargo check` is now part of every verification pass and produces **0 warnings**.

### B2 — The composer was pushed off the bottom of the window

**Found by:** running the live UI and stacking two pending approvals, then noticing the Send button
had vanished.

**Cause:** `.chat__scroll` used `flex: 1; overflow-y: auto` without `min-height: 0`. A flex child
cannot shrink below its content's intrinsic height, so the tall approval stack grew the page instead
of scrolling, pushing the footer below the fold.

**Fix:** `min-height: 0` on `.chat__scroll`, plus `overflow: hidden` on `.chat` / `.chat__main`,
`min-height: 0` on `.chat__sidebar`, and the approvals list capped at `34vh`.

**Guard:** measured, not eyeballed:

```js
{ chatH: 780, composerBottom: 780, docScrollHeight: 780, innerHeight: 780 }
```

`docScrollHeight === innerHeight` proves the page no longer scrolls, and `composerBottom === viewport`
proves the composer is pinned to the bottom edge.

### B3 — Plans lied about their own progress

**Found by:** a completed task rendered every step with the `PENDING` glyph `○`.

**Cause (two halves, both had to be fixed):**
1. **Backend.** `task.steps` was only written on completion or failure. At an approval gate the
   orchestrator persisted `task.result` but not `task.steps`, so `GET /tasks/{id}` returned
   `steps: []` for a paused task — the four steps that had already run were invisible.
2. **Frontend.** The bubble kept the plan snapshot from the `plan_created` event, and nothing ever
   folded the task's real per-step statuses back into it.

**Fix:** a `step_records()` helper in the orchestrator, now called on completion, failure, block,
shadow, **and at the approval gate**; plus `mergeStepStatuses()` in the store, which joins
`TaskStepRecord.sequence` to `PlanStep.index`.

**Guard:** assertions added to the existing approval test, so the regression cannot come back
silently:

```python
paused_statuses = [step["status"] for step in paused["steps"]]
assert paused_statuses, "a paused task must persist its steps"
assert "WAITING_APPROVAL" in paused_statuses
assert paused_statuses.count("PENDING") < len(paused_statuses), \
    "steps that already ran must not still read as pending"
```

Confirmed live afterwards: `task_153d6ecd3171 WAITING_APPROVAL → 0 WAITING_APPROVAL | terminal_run`.

### B4 — The toast covered the Send button

**Found by:** running a background completion in the chat window; the toast rendered at
`bottom: 16; right: 16` and sat on top of Send.

**Fix:** `Toast` gained `placement` (`bottom-right` default for the dashboard, `top-right` for chat,
clear of the header).

### B5 — The user's own `.env` leaked into the test suite

**Found by:** adding `REQUIRE_WRITE_APPROVAL=true` to `.env` and re-running the suite — **3 unrelated
tests broke**:

```
FAILED tests/test_orchestrator.py::test_file_creation_is_executed_and_verified
FAILED tests/test_orchestrator.py::test_dot_status_reflects_progress
FAILED tests/test_security.py::test_shadow_mode_does_not_change_verdicts
```

**Cause:** the `settings` fixture builds a real `Settings()`, which reads the repo-root `.env`. Tests
were asserting the specification's default (MEDIUM auto-executes) while silently inheriting a
developer's personal preference. The `conftest` docstring already warned about exactly this class of
problem for other keys.

**Fix:** the fixture now pins `REQUIRE_WRITE_APPROVAL=false`, and the new tests enable strict mode
explicitly. `106 passed`.

**Why this one matters:** it is a latent defect that had nothing to do with the feature being built.
Anyone with a non-default `.env` would have seen a red suite and blamed the wrong code.

---

## 6. How it was tested

Seven layers, cheapest first. A layer is only trusted for what it can actually prove.

### Layer 1 — Static analysis

```bash
cd backend && uv run ruff check app tests      # → All checks passed!
cd desktop && npm run typecheck                 # → tsc --noEmit, clean
cd desktop && npm run build                     # → 64 modules, 219.12 kB JS / 66.23 kB gzip
cd desktop/src-tauri && cargo check             # → Finished, 0 warnings
cd desktop && npx tauri info                    # → ✔ WebView2, MSVC, rustc, cargo
```

### Layer 2 — Unit and integration tests

```bash
cd backend && uv run pytest -q                  # V1: 102 → V2: 106 → V2.5: 137 passed
```

Tests run **offline and isolated**: `conftest.py` points `HOME`/`USERPROFILE` at a `tmp_path`, so the
suite can exercise `~/Downloads` plans without ever touching the developer's real filesystem. That
isolation is what made the V1 approval tests safe, and it is what B5 was a hole in.

### Layer 3 — Contract inventory

```bash
curl -s http://127.0.0.1:8756/openapi.json | python -c "... print every path"
```

Used to confirm every endpoint the README documents actually exists. Programmatically counted from
the generated schema: **38 paths / 46 operations** across reasoning, screen, research, tasks,
approvals, automations, memory, skills, security, observability and settings.

### Layer 4 — Live backend

```bash
curl -s http://127.0.0.1:8756/health
```

```json
{"status":"ok","providers":{
  "nemotron":"connected","tavily":"connected",
  "memory_store":"local-file-store","vectors":"local-vectors",
  "embedder":"nebius:Qwen/Qwen3-Embedding-8B",
  "agent_runtime":"dobot-local-tools:available",
  "sandbox":"local:none","shadow_mode":"off","screen_capture":"on_demand"},
 "degraded":[]}
```

`degraded: []` with real keys present is the meaningful result — it proves the providers answered,
not merely that configuration was loaded.

### Layer 5 — Live end-to-end, with expected outcomes written down first

Four real requests through the running UI. The point was to predict the verdict and the model tier
*before* sending, then check.

| # | Request | Expected | Observed | Evidence |
| --- | --- | --- | --- | --- |
| 1 | "Clean up my Downloads folder" | plan → mixed ALLOW + one APPROVAL | `WAITING_APPROVAL`; two `fs_move` at MEDIUM/ALLOW ran, the installer move was gated at HIGH | activity log `19:17:34–35`; approval card `fs_move`, `HIGH`, preview `28 file(s), 3.6 GB` |
| 2 | same prompt again | completes via the skill | `COMPLETED`, verification `2 checks passed` | `19:18:48–19:19:02`; step `skill_run` `COMPLETED` |
| 3 | "How many files are in my Downloads… do not change anything" | read-only, but the route it picks is shell → should be gated | `WAITING_APPROVAL`, `terminal_run` | reason: `unknown_terminal_binary_requires_approval` — *"Command uses a binary outside the reviewed allowlist"* |
| 4 | "Say hello in one short sentence…" | direct answer on a cheap tier | `COMPLETED` on `nvidia/nemotron-3-super-120b-a12b · super` | rendered in the UI |

Request 3 is the deliberate falsification attempt: it confirmed the allowlist genuinely refuses a
PowerShell one-liner rather than quietly running it.

### Layer 6 — UI verification against the running app

Not "it looks fine" — assertions:

- **Structure:** accessibility snapshot of the chat window listing the sidebar, nav, four suggestion
  cards, composer chips and disabled-when-empty Send.
- **The toggle:** clicking the switch and reading the result —
  `{alwaysOn: "true", checked: true, banner: true}` — proving state, persistence and the banner move
  together.
- **Layout:** the numeric layout probe from B2.
- **Rendered content:** DOM assertions on the Security page —
  `sectionHeadings: ["Sandbox","Human control","Confirmation","Boundaries","Policies"]`,
  `confirmationChip: ["ON"]`, `isWarnStyled: "notice notice--warn"`, `pageScrolls: false`.
- **Visual:** screenshots of the welcome state, a thread with plan + approval card, and the Security
  page.

### Layer 7 — Packaging

`npx tauri build --debug` (§4) — the only test that proves the app can become something a person
installs.

### Cross-validation worth noting

The earlier session's **shadow-mode** previews predicted `117 file(s), 79.6 MB`,
`304 file(s), 198.8 MB` and `28 file(s), 3.6 GB`. When the same task later ran for real, the counts
were **exactly** 117 and 304. The preview is therefore an accurate predictor of what execution will
do — which is the entire justification for shadow mode existing.

---

## 7. Declared vs. actual posture

| Claim | How to check it | Result |
| --- | --- | --- |
| Continuous monitoring is off | `GET /security/status` → `continuous_monitoring` | `false` |
| Screen capture is on-demand only | `/health` → `screen_capture` | `on_demand` |
| Kernal-level isolation is **not** active | `/security/status` → `sandbox.isolation` | `none` — surfaced honestly in the UI, not hidden |
| `HIGH`/`CRITICAL` never auto-run | approval test + request 1 above | installer move gated |
| Strict write confirmation | `require_write_approval` after editing `.env` | `True` |

Live check of the strict mode against the real configured paths:

```
require_write_approval from .env: True
  fs_move    risk=MEDIUM   verdict=APPROVAL
  fs_write   risk=MEDIUM   verdict=APPROVAL
  skill_run  risk=MEDIUM   verdict=APPROVAL
  fs_list    risk=LOW      verdict=ALLOW
```

Two design decisions were made deliberately here, and both are worth re-examining in V3:

- **Reads stay automatic.** `fs_list` / `fs_read` are not changes, so gating them would turn
  "what's in my Downloads?" into a click queue. The setting gates *changes*, not inspection.
- **`skill_run` is inside the gate.** An unexpanded skill executes its whole workflow under a single
  decision — that is precisely how request 2 moved 421 files under one MEDIUM `ALLOW`. Omitting
  `skill_run` would have made the setting decorative.

---

## 8. Incidents and lessons

### 8.1 The live test mutated real user data

**What happened.** While verifying the UI, request 1 was sent with shadow mode off and no approval
prompt for MEDIUM actions — which is the correct, documented default. The action firewall did its
job exactly as designed: two reversible `fs_move` steps ran, and the irreversible 3.6 GB installer
move was held at a `HIGH` approval. The result was **421 real files moved** in the user's
`Downloads` folder (304 documents, 117 screenshots into `Archive/Documents` and
`Archive/Screenshots`). The `HIGH`-risk installer step never ran — `Archive/Installers` was never
created.

**Why it was wrong anyway.** The user asked for a UI review, not for their Downloads folder to be
reorganised. A default that is correct for the product is not automatically correct for *my* test
run. The test should have been in shadow mode.

**Remediation.** All 421 files were moved back to the top level (554 top-level files, zero name
collisions), the empty `Archive/` tree was removed, and the layout was confirmed identical to the
pre-test state. The user then chose the stricter posture, which was implemented (§3.4) and enabled.

**Lesson.** When you drive a real system that can act on real data, the *test harness* must be the
conservative party — shadow mode, a scratch directory, or an isolated profile — regardless of what
the product's default is.

### 8.2 Test isolation was incomplete

Covered as B5. `conftest.py` had already isolated the filesystem, the state directory and the
network, but not the *decision policy*. A single new setting in `.env` broke three unrelated tests.
The lesson is that "isolated test environment" has to enumerate every input that can change
behaviour, not just the obvious ones.

### 8.3 A half-applied refactor shipped to review

The tree at the start of the session could not compile. Nothing was committed, so nothing was lost,
but the lesson is that a refactor touching a window/state contract should be finished or reverted
before handing the workspace over — a tree that half-compiles is worse than either endpoint.

---

## 9. What is NOT verified

Stated plainly, because a false green is worse than a red.

| Claim | Status | Why |
| --- | --- | --- |
| The native app runs and looks right when installed | **Unverified** | The bundle was built but never installed and launched. The web UI was verified in a browser against the live backend; the shell was verified to compile and bundle. |
| The dot window's on-screen appearance, drag and position-restore | **Unverified** | Reasoned from the window/state configuration and window-event handlers, not observed. |
| Global hotkeys fire on a real desktop | **Unverified** | Registration is exercised at startup; the chords were not pressed natively. |
| Tray menu actions | **Unverified** | Compiled; not clicked. |
| Native region capture (`xcap`) on this display | **Unverified** | The browser fallback path was exercised; the Rust capture path was not. |
| Release-profile build and installer | **Unverified** | Only `--debug` was built (§4). |
| Behaviour after a genuine Windows reboot (autostart + dot restore) | **Unverified** | Requires a reboot. |
| Memory recall over a large corpus | **Unverified** | Verified only at small scale with the local store. |
| MongoDB / Zilliz paths | **Unverified** | Neither service was configured; the local fallbacks are what ran. |
| NemoClaw / OpenShell isolation | **Unverified** | Not installed — `isolation: none` is the honest current state. |
| Hermes CLI / remote runtime and `computer_*` tools | **Unverified** | Hermes is not installed; those tools refuse rather than simulate, which is the designed behaviour but was not observed. |
| The V2.5 guards against a *real* evasion attempt | **Partly verified** | The rules are unit-tested against real payload strings (encoded commands, reverse shells, download-and-execute, webhooks, private keys) and against ordinary commands for false positives. No adversarial red-team pass was run, and no obfuscation beyond the coded patterns was attempted. |
| The skill scanner against a real malicious skill | **Unverified** | Tested against synthetic payloads in `tmp_path`. No third-party skill was downloaded and scanned. |
| The mode control clicked in the running UI | **Unverified** | Backend behaviour is covered by orchestrator tests; the segmented control is `tsc`-clean but was not clicked against a live backend in this round. |
| `SKILL_SCAN_MODE=block` as a default | **Judgement call** | It is the more useful default, but B6 shows how easily a false positive becomes a silently disabled feature. `warn` is the safer default if you are not auditing the findings. |

The **first five rows** are the highest-value gaps and are exactly what V3 should close.

---

## 10. Reproducing the verification

```bash
# 1. static
cd backend && uv run ruff check app tests
cd desktop && npm run typecheck && npm run build
cd desktop/src-tauri && cargo check

# 2. tests
cd backend && uv run pytest -q                       # expect 106 passed

# 3. live
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8756
curl -s http://127.0.0.1:8756/health                 # expect degraded: []
curl -s http://127.0.0.1:8756/security/status        # inspect the posture
cd desktop && npm run dev                            # UI at http://127.0.0.1:1420

# 4. packaging
cd desktop && npx tauri build --debug                # expect 2 bundles

# 5. native shell (the gap V3 should close)
cd desktop && npm run tauri dev
```

**Use shadow mode when testing anything that touches real files.** Turn it on in the composer or set
`SHADOW_MODE=true`, or test against a scratch directory. Do not repeat the mistake in §8.1.

---

## 11. V2.5 — hardening derived from the reference study

Three projects were studied as sources of ideas, not as code to copy. Each is a different lineage of
personal-assistant design, and each named something Dobot was missing.

### 11.1 What was taken from where

| Source | Idea | What Dobot did with it |
| --- | --- | --- |
| **Leon 2.0** | Three execution modes: `smart`, `controlled`, `agent` | Became the Ask / Assist / Agent ladder (§11.2), implemented as a real approval threshold rather than a label. |
| **Leon 2.0** | Progressive tool-surface loading; a compact self-model; a bounded proactive pulse | Not built. Recorded in §13. |
| **QwenPaw** | Five named security layers: Sandbox, Tool Guard, File Guard, Skill Scanner, Access Policy | Three of the five were genuinely absent (File Guard, Skill Scanner, shell-evasion rules), and Access Policy existed only as a flat list. Built all four missing pieces. |
| **QwenPaw** | Approval levels `STRICT / SMART / AUTO / OFF` | Became the three-mode ladder plus the existing `REQUIRE_WRITE_APPROVAL` floor. |
| **QwenPaw** | Tool Guard's `ShellEvasionGuardian` — "command injection, path traversal, reverse shells, obfuscated attacks" | Became the two-tier shell-evasion policies (§11.4). |
| **QwenPaw** | Kernel sandbox on Windows via **AppContainer** | Not built, but it is now the concrete answer to the `isolation: none` gap that V1 and V2 both reported. Recorded in §13. |
| **awesome-openclaw-usecases** | A machine-readable catalog with per-use-case **risk labels**, and an `AGENTS.md` protocol that computes *the minimum a human must do, and when* | Not built; the "minimum human action" idea is the most interesting unexplored direction. Recorded in §13. |
| **awesome-openclaw-usecases** | Explicit warning that third-party skills are unaudited | Directly motivated the skill scanner — this is the documented reason it is needed, stated by the community itself. |

The two things deliberately **not** taken: QwenPaw's multi-channel reach (DingTalk/WeChat/Discord/QQ)
and its plugin marketplace. Both are large, and neither addresses a gap that matters more than the
security ones.

### 11.2 Execution modes — one enum, three real behaviours

The mode a message runs under **is** the approval threshold
(`MODE_APPROVAL_THRESHOLD`, `app/core/decision_engine.py`):

| Mode | Threshold | Behaviour |
| --- | --- | --- |
| **Ask** | — | Answers and shows the plan; executes nothing *by construction*. |
| **Assist** | `MEDIUM` | Anything beyond a read is confirmed. |
| **Agent** *(default)* | `HIGH` | Safe reversible work proceeds; `HIGH`/`CRITICAL` stop for you. |

Three properties made this safe to ship in one pass:

1. **The default is unchanged.** `agent` has exactly the V1/V2 behaviour, so no existing test moved and
   nobody's workflow changed without opting in.
2. **The ladder is monotonic.** Policies, JEV and `REQUIRE_WRITE_APPROVAL` can only make a decision
   *stricter* than the mode allows — a mode is a ceiling on autonomy, never a bypass. There is a test
   for exactly that (`test_strict_writes_still_win_over_a_permissive_mode`).
3. **Ask mode is not a second code path.** It is expressed as shadow mode internally, so there is one
   path that cannot act rather than two that both must be got right.

`ask` thresholds to `HIGH` rather than to `LOW` on purpose: since nothing will run, a preview that
reported "approval required" for every action would be noise about moot decisions.

### 11.3 File guard

`PROTECTED_PATHS`, deliberately independent of `SANDBOX_ALLOWED_PATHS` — which defaults to the whole
home directory and therefore protects nothing of value. Defaults cover `~/.ssh`, `~/.aws`, `~/.gnupg`,
`~/.kube`, `~/.netrc`, `~/.config/gh`, `~/.config/gcloud`, `~/.docker/config.json`.

It blocks **reads as well as writes**, and matches two shapes: a tool carrying a real path, and a
shell command that merely mentions one. Before this, `fs_read ~/.ssh/id_rsa` was **ALLOW**: the only
matching policy was `credential_change_requires_approval`, which is `APPROVAL` and did not cover
reads at all.

### 11.4 Shell evasion

The command guards were a literal list of dangerous verbs, which is trivial to walk around. Two new
tiers look for the shape of an evasion attempt:

- **BLOCK** — payload piped into a shell (`… | sh`, `-EncodedCommand`, `FromBase64String`),
  fetch-and-execute, reverse shells (`nc -e`, `/dev/tcp/`, `socat … exec:`), `certutil -decode`,
  audit tampering, persistence, transmitting a private key.
- **APPROVAL** → `HIGH` — `eval(…)`, `$IFS` splitting, hex escapes, `chmod +x && ./…`, inline sockets.

A test asserts `dir`, `git status`, `python -m pytest` and `npm run build` still evaluate to `ALLOW`,
because an evasion detector that fires on ordinary work is worse than none.

### 11.5 Skill scanner

`app/security/skill_scanner.py`. Every skill is scanned at load; the status is attached to the record,
surfaced on the **Skills** page, and summarised on **Security**. Enforcement is deliberately twofold:

1. a blocked skill is excluded from `matching()`, `prompt_section()` and `active()`, so the planner can
   never propose it; and
2. `skill_run` naming a blocked skill is `BLOCK`ed by the `blocked_skill_refused` policy, so it cannot
   be run directly either.

`records()` still returns it, because the UI must be able to show you what was refused and why. This
matters: reporting a finding without preventing the action is not a control.

### 11.6 Access policy, at tool granularity

`GET /security/permissions` now returns, per capability: its declared risk floor, whether that floor
runs unattended in the active mode, whether a condition can make it ask, whether one can make it
refuse, and which guards apply. The **Security** page renders it as a matrix. This closes the gap where
`require_confirmation_for` was a hand-written list of five words rather than a computed posture.

### 11.7 Verified live

Against the running backend and the real `.env`, not test doubles.

**The three modes, end to end** (step statuses read back from the response):

| Request | Mode | Status | Step | File created |
| --- | --- | --- | --- | --- |
| "create a file called ask-notes.md" | ask | `WAITING_USER` | `fs_write` **PENDING** | **no** |
| "create a file called assist-notes.md" | assist | `WAITING_APPROVAL` | `fs_write` WAITING_APPROVAL, risk `MEDIUM` | **no** |
| "create a file called ask-notes.md" | *(omitted → agent)* | `WAITING_APPROVAL` | `fs_write` WAITING_APPROVAL, risk `MEDIUM` | **no** |

Ask mode's answer began `ASK MODE — here is what I would do. Nothing has been executed.` and nothing
was. The third row is the monotonicity rule doing its job: this user's `REQUIRE_WRITE_APPROVAL=true`
floor is stricter than both `agent` and `assist`, so the two modes currently collapse for file
changes. Correct, but worth knowing — driving strictness from the mode requires turning that setting
off.

**The guards, against the resolved protected roots:**

```
BLOCK     HIGH      private key, read        (fs_read)
BLOCK     CRITICAL  cloud credentials, read  (fs_read)
ALLOW     LOW       ordinary read            (fs_read)
BLOCK     CRITICAL  key via shell            (terminal_run)
BLOCK     CRITICAL  secret file via shell    (terminal_run)
BLOCK     HIGH      encoded payload          (terminal_run)
BLOCK     MEDIUM    download and run         (terminal_run)
BLOCK     MEDIUM    reverse shell            (terminal_run)
BLOCK     MEDIUM    audit tampering          (terminal_run)
APPROVAL  HIGH      suspicious eval          (terminal_run)
APPROVAL  MEDIUM    ordinary command         (terminal_run)
ALLOW     LOW       ordinary listing         (fs_list)
```

Before V2.5 the first line was `ALLOW`. The eleventh line is the friction noted in
`docs/security.md`: `terminal_run` counts as mutating, so with strict writes on, even `git status`
asks. Dobot cannot prove a shell command is read-only without parsing it, and being wrong in the
permissive direction costs more.

**Defence in depth, observed by accident.** Asking Dobot in plain language to read
`~/.ssh/id_rsa` produced a refusal from **Nemotron itself** — zero steps planned — before the file
guard was ever consulted. Worth recording precisely because it means the guard was *not* exercised on
that path; it is verified by the matrix above and by the unit tests, not by that request.

**The UI, by DOM assertion rather than screenshot** (the preview lost its compositor late in the
session):

- Security page headings, in order: `Sandbox`, `Human control`, `Confirmation`, **`File guard`**,
  **`Skill scanner`**, **`Access policy`**, `Boundaries`, `Policies`.
- `fs_read` row: floor `LOW`, unattended `runs`, `can refuse`, guards
  `protected_path_blocked, path_outside_sandbox`.
- `terminal_run` row: floor `MEDIUM`, `asks`, `can refuse`, guards include `shell_evasion_blocked`,
  `shell_evasion_suspicious` and `blocked_skill_refused` — 11 guards in total.
- Mode control: three labels `Ask / Assist / Agent`, default `Agent`; clicking **Ask** persisted
  `dobot.mode = "ask"`, moved the active pill, changed the hint to *"Answer and show the plan. Nothing
  is executed, ever."* and the composer placeholder to *"Ask a question — Ask mode runs nothing…"*.
- All three shipped skills report `clean`; the Skills page shows a refusal banner only when one is
  actually blocked.

---

## 12. Bugs found in V2.5

Both were found by running the new code against the real repository rather than by reading it.

### B6 — The skill scanner disabled one of Dobot's own skills

**Found by:** running the scanner against `skills/` immediately after writing it.

```
clean_downloads      clean      0 finding(s)
research_topic       clean      0 finding(s)
weekly_report        blocked    1 finding(s)
    [critical] dangerous_command @ SKILL.md: disk or shadow-copy destructive command
```

**Cause:** the scanner restated the dangerous-command patterns by hand instead of reusing them, and
the copy used a bare `\bformat\b`. That matches the word **"format"** in ordinary prose. The policy
layer's own version had always required a drive letter (`\bformat\s+[a-z]:`) — the duplication had
drifted.

**Why it mattered:** with `SKILL_SCAN_MODE=block` as the default, shipping this would have silently
removed a working skill from every installation. A security control that disables the product is a
bug, not a feature.

**Fix:** the scanner now imports `DESTRUCTIVE_COMMAND_PATTERNS` and `SHELL_EVASION_BLOCK_PATTERNS`
from the policy layer. One source of truth for what "dangerous" means.

**Guard:** `test_every_shipped_skill_survives_the_scanner` asserts all three shipped skills are active,
and `test_destructive_word_in_prose_is_not_a_finding` pins the specific false positive.

### B7 — The file guard missed `~`-relative commands

**Found by:** a test expecting `cat ~/.ssh/id_rsa` to be `BLOCK`; it came back `APPROVAL`.

**Cause:** two independent holes in the text matcher.

1. The roots are resolved absolute paths (`C:\Users\…\.ssh`), while a command is written the way a
   person writes it (`~/.ssh/id_rsa`). Comparing only the absolute form matched neither.
2. The basename fallback used `(^|[\s"'])name`, so a path *suffix* did not count as a boundary —
   `/tmp/x/id_rsa` never matched `id_rsa`.

**Fix:** compare against both spellings of every root (resolved, and `~`-form derived by subtracting
`Path.home()`), and allow path separators as basename boundaries.

### B8 — My own new test hit the `.env`-leak bug I had documented as B5

**Found by:** `test_modes_shift_what_runs_unattended` failing: `agent` mode returned `APPROVAL` for a
reversible move.

**Cause:** the test built a bare `Settings(execution_mode=mode)`, which reads the developer's real
`.env` — and this user's `.env` now sets `REQUIRE_WRITE_APPROVAL=true`, so `writes_require_approval`
fired and gated the move.

This is precisely the defect recorded as B5, reappearing in new code two hours after being documented.
That is the real lesson: pinning keys in a fixture only helps tests that *use* the fixture, and writing
`Settings(...)` directly is how the leak gets back in.

**Fix:** the mode tests now build from the `settings` fixture via `model_copy(update={...})`, so they
inherit the pinned environment and override only the field under test.

---

## 13. V3 — proposed

Ordered by value, not effort.

1. **Close the native verification gap.** Install the built bundle, launch it, and observe: chat opens
   by default, the dot appears only when toggled, hotkeys fire, the tray works, the mode control
   responds, and the dot's position survives a restart. Cheapest large win: the artefacts exist.
2. **AppContainer sandbox on Windows.** QwenPaw names it as the Windows equivalent of Seatbelt /
   Bubblewrap. It is the concrete route to replacing `isolation: none` with real kernel-level
   isolation, and it is the single most valuable unbuilt item from the reference study.
3. **Progressive tool-surface loading** (Leon). Dobot sends all 26 tools on every planning call. Loading
   only what a turn needs should improve tool-selection reliability and cut tokens.
4. **A "minimum human action" briefing** (OpenClaw's `AGENTS.md` protocol). When a plan needs
   approval, say exactly what the human must do and *when* — "approve this one step, then the
   remaining four run without you" — rather than presenting a list and leaving the user to infer it.
5. **A risk-labelled use-case catalog.** The 50-use-case collection is organised by pain, capability,
   required skills, setup and difficulty. A Dobot equivalent would be far more useful than a feature
   list for answering "what should I actually use this for?".
6. **Ship the backend as a Tauri sidecar.** Bundle the Python gateway so one installer is genuinely
   one-click. `docs/install.md` currently documents four ways to keep the backend running; that is the
   biggest remaining gap between "built" and "usable".
7. **Bump to `0.2.0`** across the four version files and rebuild, so artefacts and source agree and
   V1 / V2 / V2.5 are distinguishable in the field.
8. **Kernel-level isolation via NemoClaw / OpenShell or AppContainer.** Until then the Security page
   must keep saying `isolation: none` — which it does.
9. **Split skill decisions per step.** Skills expand only for the shadow *preview*; at execution time
   the whole workflow still rides one `skill_run` verdict. Expanding them properly would give per-step
   risk, per-step approval and per-step verification, and would retire the `skill_run` trust shortcut.
10. **Authenticate the gateway.** A loopback bearer token or a named pipe, so "it's only local" stops
    being the entire security argument.
11. **Adversarial testing of the new guards.** The evasion rules were written from a mental list of
    attack shapes. They deserve a red-team pass with obfuscation the authors did not think of, and a
    real third-party skill scanned end to end.
12. **Release build with signing.** `npm run tauri build` plus a certificate, to remove the SmartScreen
    warning documented in `docs/install.md`.
13. **Voice**, the optional-specification item, as an input surface on top of the existing composer.
14. **Test isolation hardening.** Make the fixture fail loudly if it picks up *any* value from the
    developer's `.env`, rather than pinning keys one at a time. B8 is the second occurrence of exactly
    this defect, which is the argument for fixing the mechanism instead of the instances.
15. **Playwright coverage of the chat flow**, so the render-level assertions in §6 Layer 6 become a
    repeatable suite rather than one-off probes.

---

## 14. Open decisions

| Decision | Current choice | Why it may change |
| --- | --- | --- |
| Memory backend conflict | MongoDB + Zilliz (product header) over Postgres + pgvector (§58) | Store interfaces are narrow enough for a third backend; if the §58 reading is authoritative, this needs swapping |
| `REQUIRE_WRITE_APPROVAL` default | `false`, matching the spec's MEDIUM-auto-executes rule | Enabled in this user's `.env`. If real usage shows silent MEDIUM changes are unwelcome, invert the default |
| Reads under strict mode | Remain automatic | If a user wants *zero* unattended access, a second flag is needed rather than overloading this one |
| Version string | Still `0.1.0` | Deliberate, see §1 |

---

## 15. Environment record

| Component | Version |
| --- | --- |
| OS | Windows 10.0.26200 (x64) |
| Python | 3.11.9 |
| uv | 0.11.7 |
| Node / npm | 26.5.0 / 11.17.0 |
| Rust / cargo | 1.97.1 |
| MSVC | Visual Studio Community 2022 |
| WebView2 | 153.0.4234.48 |
| Tauri | 2.11.6 (`tauri-build` 2.6.3, JS API 2.11.1, CLI 2.11.5) |
| Wry / Tao | 0.55.1 / 0.35.3 |
| Frontend | React 18.3.1, Vite 5.4.21, TypeScript 5.6.3, zustand 5.0.2 |

Verification servers were stopped after each pass (ports 8756 and 1420 confirmed free), and the
throwaway backend state directory `.dobot/` created by the live tests was removed.
