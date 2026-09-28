# Dobot

**An always-on personal AI operating layer for your computer.**

> An AI that doesn't just answer you. It works for you.

Chat assistants make you do the work twice: you switch windows to find them, re-explain what you were
looking at, and then copy the answer back out. Dobot closes that loop. It can see the thing you
selected on screen, understand what you actually want, plan the steps, ask before anything risky,
operate your computer, verify that it worked, and remember the result for next time.

```
SEE → UNDERSTAND → DECIDE → ACT → VERIFY → REMEMBER
```

**What makes it different**

- **It acts, it does not just answer.** Plan → approve → execute → verify, and the answer carries
  the evidence that the work happened.
- **Safety is code, not prompt.** A deterministic decision engine sits between the model and your
  machine: the model states intent, policy decides what may run.
- **Local by default.** Screen capture only inside an explicit gesture, speech transcribed on-device,
  credentials held by the backend and never shipped to the UI.
- **Two surfaces.** An ordinary chat window, or an always-on dot you can turn on when you want Dobot
  within reach from every application.

**Contents:** [Two ways to live](#two-ways-to-live-with-it) ·
[What it can do](#what-it-can-actually-do) · [Architecture](#architecture) ·
[Quickstart](#quickstart) · [Hotkeys](#global-hotkeys) · [API](#backend-api-surface) ·
[Security](#security-posture) · [Status](#status)

Built for the NVIDIA × Nebius **Personal AI** track: **NVIDIA Nemotron** reasoning served through
**Nebius Token Factory**, with **Hermes** as the agent runtime, **NVIDIA NemoClaw / OpenShell** as the
controlled execution boundary, and **Tavily** for source-aware web research — behind a deterministic
**decision engine** that decides what may run without asking.

---

## Two ways to live with it

Dobot ships with two personalities, and **you choose which one is on screen**.

**1. A normal chat window (the default).** Launch Dobot and you get an ordinary desktop assistant:
a sidebar of conversations and workspace pages, a message thread with plans, sources and approvals
inline, and a composer at the bottom. Nothing floats. Nothing follows you. It behaves like the chat
apps you already use.

**2. The always-on dot (opt-in).** Flip **Always on** in the chat header and a small floating dot
appears above every window. Drag it anywhere; it remembers where you left it. Click it for a compact
quick-ask panel. It changes colour to tell you what it is doing — thinking, executing, waiting for
your approval — so its state is readable at a glance without opening anything.

Turn **Always on** off again and the dot disappears; Dobot is once more just a chat window. The
switch is in the chat header, in **Security → Human control**, in **Settings → Preferences**, and in
the tray menu. The choice is remembered across restarts.

---

## What it can actually do

- **Ask, plan, answer.** A question that needs no action is answered directly, on the cheapest model
  tier that can handle it.
- **Choose how much rope it gets.** Every message runs in a mode — **Ask** (answer and plan, execute
  nothing), **Assist** (confirm anything beyond a read), or **Agent** (the default: safe reversible
  work proceeds, the risky stops for you). The mode *is* the approval threshold, and it is remembered
  per message and persisted across a paused plan.
- **Look at your screen.** Select a region with `Ctrl+Shift+S`, or hand over the window you are in
  with `Ctrl+Alt+L` and check the preview before anything goes. Capture happens only inside that
  explicit gesture — there is no continuous monitoring.
- **Talk to it from anywhere.** Hold `Ctrl+Shift+Space` and speak: the quick panel comes up already
  listening, a waveform shows it hears you, and letting go sends what you said. The audio is
  transcribed locally by Whisper and never leaves the machine.
- **Take what you hand it.** Attach images (4 × 5 MB) or text and code files (4 × 256 KB) with a drag,
  a paste or the paperclip; images go to the vision model and come back as described context, so a
  screenshot or a log file can be reasoned about, remembered and searched like anything else.
- **Research the web.** Dobot turns a request into queries, retrieves sources with Tavily, and returns
  an answer that cites them.
- **Do things.** Create and move files, run allow-listed terminal commands, open applications, drive a
  browser, and run reusable skills — each step classified and authorised before it executes.
- **Ask first when it matters.** Anything irreversible or shell-level stops at an approval card that
  shows what will happen, at what risk, and with a concrete preview (file counts, byte totals,
  destinations) rather than a vague "allow?".
- **Verify itself.** Every step records the checks that prove it worked — or that it could not be
  independently verified. It never reports success it did not confirm.
- **Remember.** Preferences, projects, people, workflows and task episodes persist and come back as
  context on later requests.
- **Automate.** Cron-style recurring tasks and reminders run through the same plan → decide → act →
  verify loop.
- **Stop instantly.** A global kill switch cancels the active task and terminates pending tool calls.

---

## Architecture

```
                                  USER
                                   │
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ DOBOT DESKTOP  (Tauri 2 + React + TypeScript)                 │
   │   chat window · always-on dot · quick panel · dashboard       │
   │   regional screen select · tray · hotkeys · notifications     │
   │   Settings → Backend points at any backend URL                │
   │   (127.0.0.1 by default; point it at your own)                │
   └───────────────────────────────┬───────────────────────────────┘
                   HTTP (commands) │ WebSocket (events)
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ DOBOT GATEWAY  (FastAPI + asyncio)                            │
   └───────────────────────────────┬───────────────────────────────┘
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ CONTEXT ENGINE                                                │
   │   screen + OCR · memory recall · open tasks · environment     │
   └───────────────────────────────┬───────────────────────────────┘
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ NEMOTRON 3 (Nebius Token Factory)   ← reasoning, planning     │
   │   ultra / super / light tiers chosen by the model router      │
   └───────────────────────────────┬───────────────────────────────┘
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ DECISION ENGINE  (authoritative, deterministic)               │
   │   risk classification · policy rules · JEV signals            │
   │   → ALLOW  |  APPROVAL  |  BLOCK                              │
   └───────────────────────────────┬───────────────────────────────┘
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ EXECUTION                                                     │
   │   Hermes agent runtime (local · CLI · HTTP)                   │
   │   NemoClaw / OpenShell boundary (sandbox · network · secrets) │
   └───────────────────────────────┬───────────────────────────────┘
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │ VERIFICATION → PERSISTENT MEMORY → ACTIVITY LOG               │
   └───────────────────────────────────────────────────────────────┘
```

Full detail, module by module: [`docs/architecture.md`](docs/architecture.md).

### Design rules that shaped it

1. **One interface per external dependency, each with a working local fallback.** With no keys and no
   services, Dobot still completes the entire loop — it just does it deterministically. The UI always
   shows which providers are really live.
2. **The model never decides safety.** Nemotron decides *what it intends to do*; deterministic policy
   code decides *whether that may happen*.
3. **Secrets stay in the backend.** The desktop app never receives a raw credential — it only asks
   about connection status.
4. **Nothing is claimed that was not verified.** Steps carry their own evidence, and "not independently
   verified" is an explicit, visible outcome.

---

## Why each piece

**Nemotron 3 through Nebius Token Factory** is Dobot's reasoning core. It handles intent
understanding, multi-step planning, research synthesis, tool selection and autonomous decomposition.
Nebius serves it over an OpenAI-compatible endpoint — `https://api.tokenfactory.nebius.com/v1`, not the
legacy `api.studio.nebius.com` host, which returns 404 for `/chat/completions`.

The models in use (all verified against `GET /v1/models`):

| Tier | Model id | Used for |
| --- | --- | --- |
| ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | complex, multi-step, autonomous work |
| super | `nvidia/nemotron-3-super-120b-a12b` | ordinary requests that still need planning |
| light | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | direct answers, trivial routing |
| vision | `openbmb/MiniCPM-V-4_5` | looking at screen content |
| embedding | `Qwen/Qwen3-Embedding-8B` | memory recall |

`backend/app/core/router.py` picks the tier so a "hello" never costs an ultra-tier call.

Latency is managed explicitly: Nemotron 3's visible chain-of-thought is **off by default**
(`chat_template_kwargs: {"enable_thinking": false}`) and is only enabled where it earns its cost —
the light tier and the final answer composition never think at all, so a direct question returns in
seconds, not minutes. Set `NEMOTRON_ALWAYS_THINK=1` to restore deep reasoning everywhere.

**Hermes Agent** is the execution layer. Dobot deliberately does not reimplement computer use: it
orchestrates Hermes for browser automation, desktop interaction, terminal commands, filesystem
operations, reusable skills and scheduled workflows. Three runtimes are supported behind one interface
(`local`, `cli`, `remote`) — see [`docs/agent.md`](docs/agent.md).

**NemoClaw / OpenShell** is the controlled execution boundary: it keeps inference and MCP credentials
outside the sandbox, applies network and filesystem policy, and manages the sandbox lifecycle.

**Tavily** supplies the research capability: screen context or a request becomes search queries,
sources are retrieved, and the answer comes back source-aware.

**JEV** is an *auxiliary* decision-intelligence layer, never a replacement for Nemotron. It contributes
signals; deterministic policies remain authoritative and always win.

**Laya** ([convaiinnovations/laya](https://huggingface.co/convaiinnovations/laya)) is the open model
behind those JEV signals. It is a non-autoregressive "System 1" judgement model — ModernBERT-large,
421 M parameters, ~33 ms per forward pass — that returns a *calibrated probability* that an action is
risky, urgent or sensitive. It never generates text, so it cannot be talked into approving something,
and it is fast enough to sit in front of every step of every plan. Dobot talks to it in three modes,
in order of preference:

1. **Server** (recommended) — install once, run as a sidecar:
   `pip install "laya[serve]" && laya-serve`, then set `LAYA_SERVER_URL=http://127.0.0.1:8000` in `.env`.
2. **In-process** — `pip install laya`, set `LAYA_INPROCESS=1` (no separate server, loads with the backend).
3. **Heuristic** (default) — with neither configured, JEV falls back to its deterministic
   blast-radius/sensitivity heuristics. Dobot stays fully functional; Doctor shows the exact fix.

Laya's score is *blended*, not obeyed: it can escalate a step to human approval, but it can never
lower a risk level the deterministic policies already assigned, and it can never approve anything on
its own. Run `doctor` in the app to see which mode is live.

**Agent harness (LangGraph).** Every tool step is driven through a structured
`validate → execute → finalise` loop. With [`langgraph`](https://github.com/langchain-ai/deepagents)-class
tooling installed the loop runs as a real state graph — an inspectable, replayable machine rather than
scattered conditionals; without it, the identical stages run inline. The harness *wraps* the decision
engine, approvals and interceptors; it never replaces them, and outcomes are identical in both modes.
`LANGGRAPH_ENABLED=false` switches it off.

**Usage monitoring (LangSmith).** With `LANGSMITH_API_KEY` set, every model call and every harness
stage becomes a searchable run in [LangSmith](https://smith.langchain.com) — inputs, outputs, token
counts, latency, errors — so "what did Dobot do all day and what did it cost?" has a real answer.
Off entirely without a key; the local run journal and cost ledger always record usage regardless.
Tracing is failure-tolerant by construction: a dead LangSmith endpoint costs a debug log line, never
a task.

**Latency.** Nemotron 3's thinking trace is the dominant cost on reasoning models. Dobot disables it
for direct questions and final answer composition (`chat_template_kwargs: {"enable_thinking": false}`),
keeps it for deep planning, and routes trivial requests to the light tier — a direct question
round-trips in ~2.5–3 s live. Set `NEMOTRON_ALWAYS_THINK=1` to always think.

**Two audiences, one Settings page.** The app asks a person for exactly the four credentials that are
theirs — `nebius`, `tavily`, `zilliz_token`, `zilliz_uri` — and stores them on whichever backend it is
pointed at. Everything infrastructure-shaped (`mongodb_uri`, the LangSmith trio, the backend's own
`dobot_api_token`, Laya) belongs to whoever *runs* that backend: it is listed read-only with where it
comes from, and `PUT /settings/keys/{langsmith,mongodb_uri,…}` answers **403 `OPERATOR_MANAGED`**.
There is also an in-app **Guide** page: one searchable topic per sidebar feature, saying where it is,
what it does and how to use it, for someone who has never seen the app.

**Attachments.** Images ride the vision model (`NEMOTRON_VISION_MODEL`, MiniCPM-V-4.5 by default) and
their description joins the same context pipeline as screen regions; text files are decoded, clipped
and fenced into the prompt. The limits live in one place (`app/core/attachments.py`) and are published
at `GET /chat/attachments` so the composer pre-checks against the same numbers the server enforces.

---

## Requirements

| | Needed for | Verified with |
| --- | --- | --- |
| Python 3.11+ and [uv](https://docs.astral.sh/uv/) | the backend | 3.11.9 / uv 0.11.7 |
| Node 20+ | the desktop UI | Node 26.5.0 / npm 11.17.0 |
| Rust 1.77+ (MSVC toolchain on Windows) + WebView2 | the native shell | 1.97.1 / WebView2 153 |
| MongoDB, Zilliz, Hermes CLI, NemoClaw CLI, tesseract | optional upgrades | — |

**None of the optional services are required.** Dobot runs fully offline-degraded with no keys at all.

---

## Quickstart

The fastest way to *see* Dobot: start the backend and the app (steps 2 and 3 below), turn on
**Always on** in the header, then try these three in order — each exercises a different half of the
loop.

1. **“Plan my day”** — planning and a direct answer, no tools touched.
2. **“Explain what's on my screen”** after `Ctrl+Shift+S` — screen context joined to the same
   request.
3. **“Tidy my Downloads folder”** in **Assist** mode — the approval card with a concrete preview,
   and nothing moving until you say so.

### 1. Configure

```bash
cp .env.example .env
```

Fill in `NEBIUS_API_KEY` (required) and `TAVILY_API_KEY` (research), and optionally
`MONGODB_URI` / `ZILLIZ_URI` / `ZILLIZ_TOKEN` (durable memory). Keys live in `.env` only, which is
gitignored — and once the app is running, **Settings → Your API keys** writes the four user
credentials there for you. Everything else has a working default — see
[`docs/prerequisites.md`](docs/prerequisites.md) for a guide to every variable, including the
optional Laya, LangGraph and LangSmith setups.

> **What to expect with an empty `.env`:** Dobot still starts — every capability that needs a key
> reports exactly what is missing (the Doctor page lists them), memory falls back to a local file
> store, and a chat message answers with a clear "offline mode" notice instead of an error. Add
> `NEBIUS_API_KEY` and restart: reasoning, attachments and research light up with no other changes.

### 2. Backend

```bash
cd backend
uv venv
uv pip install -e ".[dev]"          # add ",mongo,vectors,ocr" for the optional stores
uv run uvicorn app.main:app --host 127.0.0.1 --port 8756 --reload
```

Check it: `curl http://127.0.0.1:8756/health` → provider status, `degraded: []` when everything is up.
Interactive API docs: <http://127.0.0.1:8756/docs>.

### 3. Desktop

```bash
cd desktop
npm install
npm run tauri dev      # real shell: chat window, tray, hotkeys, screen capture, the dot
npm run dev            # browser-only UI against the backend on :8756
```

Dobot opens as a **chat window**. Turn on **Always on** in the header if you want the floating dot.

### 4. Tests

```bash
cd backend && uv run pytest -q      # 275 tests
cd backend && uv run ruff check app tests
cd desktop && npm run build          # tsc --noEmit + vite build
```

There is also a scripted end-to-end check that exercises the real loop against whatever providers you
configured: `uv run python -m app.selftest`.

### Installing it as a real application on your laptop

See **[`docs/install.md`](docs/install.md)** — it opens with the three ways to run Dobot (from
source, the `.exe` with a local backend, the `.exe` with a hosted backend), then covers building
the installer, autostart, where your data lives and uninstalling. Every build defaults to
`http://127.0.0.1:8756`; **Settings → Backend** points the app at any other backend you run — your
own server or container (§5.5 there) or your own Hugging Face Space (§5.6). The installer ships
**without** a backend inside it; a PyInstaller sidecar exists but is opt-in
(`DOBOT_BUNDLE_SIDECAR=1`), because the bundled one crashed the laptop it was first tried on.
Remember that a backend on another machine is where local-execution tools act.

---

## Global hotkeys

| Hotkey | Action |
| --- | --- |
| `Ctrl+Shift+Space` | Talk — the quick panel opens already listening; let go (or tap again) to send |
| `Ctrl+Alt+L` | Show Dobot the window you are in — preview it in the composer before it goes |
| `Ctrl+Shift+S` | Select a screen region to ask about |
| `Ctrl+Alt+D` | Dashboard |
| `Ctrl+Shift+Esc` | Kill switch — stop the active task immediately |

The tray menu offers the same actions plus **Always-on dot**, **STOP Dobot** and **Quit**. Autostart
with Windows is a toggle in **Security** and **Settings**.

---

## Backend API surface

Local, loopback-bound, and designed for the desktop app.

| Area | Endpoints |
| --- | --- |
| Reasoning | `POST /chat`, `GET /chat/tools`, `GET /chat/attachments` (the upload contract the composer mirrors) |
| Voice | `POST /voice/speak`, `POST /voice/transcribe`, `GET /voice/transcribe/status` |
| Screen | `POST /screen/analyze`, `/screen/context`, `/screen/ocr`, `GET /screen/privacy` |
| Research | `POST /research`, `GET /research/sources` |
| Tasks | `GET,POST /tasks`, `GET,DELETE /tasks/{id}`, `POST /tasks/{id}/run`, `/tasks/{id}/cancel` |
| Approvals | `GET /approvals`, `GET,POST /approvals/{id}` |
| Automations | `GET,POST /automations`, `PATCH,DELETE /automations/{id}`, `POST /automations/{id}/run` |
| Memory | `GET,POST /memory`, `DELETE /memory/{id}`, `GET /memory/recall`, `/memory/stats`, `POST /memory/prune` |
| Skills | `GET,POST /skills`, `DELETE /skills/{name}`, `POST /skills/{name}/run` |
| Security | `GET /security/status`, `/security/policies`, `/security/permissions`, `/security/skills`, `/security/active`, `POST /security/kill` |
| Observability | `GET /health`, `/dashboard`, `/activity`, `/activity/{task_id}`, `/debug/trace` |
| Settings | `GET,PATCH /settings`, `GET /settings/providers`, `/settings/onboarding`, `GET /settings/keys`, `PUT,DELETE /settings/keys/{name}` (user keys only — operator values answer 403) |
| Auth | `GET /auth/status` |
| Live events | `ws://127.0.0.1:8756/ws` (recent events replayed on connect), plus `GET /events?after=<seq>` — the same stream over HTTP, for hosted backends whose proxy refuses the WebSocket upgrade |

---

## Security posture

- **Capture is never continuous.** `SCREEN_CAPTURE=on_demand` is the default; pixels are taken only
  during an explicit region selection and never written to disk.
- **Every action is classified** `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` and evaluated by the policy
  engine before it reaches the agent runtime.
- **`HIGH` and `CRITICAL` require explicit human approval.** `CRITICAL` never auto-runs. Approvals show
  a concrete preview, not a prompt.
- **`REQUIRE_WRITE_APPROVAL=true` makes it stricter still** — every mutating tool (files, terminal,
  skills, GUI actions) stops for approval even at `MEDIUM` risk, while reads stay automatic.
- **Paths, hosts and timeouts are enforced in-process** even when no sandbox is running.
- **Shadow mode** plans and previews everything and executes nothing — the safest way to try a
  dangerous-sounding request. **Ask mode** is the same guarantee per message.
- **A file guard** keeps credential stores (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.netrc`,
  cloud config) off limits to tools — **including reads**, because reading a private key is a
  credential disclosure even though it changes nothing. It is independent of the workspace roots,
  which are usually just your whole home directory.
- **Shell evasion detection** looks for the *shape* of an evasion attempt rather than a list of
  dangerous verbs: payloads piped into a shell, `-EncodedCommand`, `curl … | sh`, reverse shells,
  `certutil -decode`, audit tampering, persistence. Obfuscated-but-plausible commands are confirmed
  instead of refused.
- **A skill scanner** treats `skills/` as the attack surface it is. A skill is prose the model obeys,
  so every one is scanned for prompt injection, hardcoded credentials, exfiltration and dangerous or
  obfuscated commands before the planner can see it. A blocked skill is hidden from the planner **and**
  refused if named directly.
- **A global kill switch** cancels the task and its pending tool calls.
- **Secrets never leave the backend**, and log lines redact known secret values.

What is *not* enforced out of the box — and how to turn it on — is documented in
[`docs/security.md`](docs/security.md) and shown live on the **Security** page.

---

## Repository layout

```
backend/                 FastAPI gateway, orchestrator, decision engine, memory, tools
  app/core/              orchestrator · harness (LangGraph) · context engine · planner · router
                         decision engine · verifier · scheduler · activity · killswitch
  app/observability/     LangSmith tracer (optional, null-object when unconfigured)
  app/agents/            nemotron · hermes · sandbox · research agent
  app/security/          risk classification · policies · JEV (Laya) · approvals
  app/memory/            manager · retriever · record store · vectors · embeddings
  app/tools/             filesystem · terminal · browser · screen · computer · apps · productivity
  app/api/               chat · screen · research · tasks · automations · approvals · memory
                         skills · settings · security · activity · websocket
  tests/                 275 tests, no network required
desktop/                 Tauri 2 + React 18 + TypeScript + zustand
  src/pages/             Chat (default) · Overview · Tasks · Automations · Approvals · Memory
                         Skills · Activity · Security · Settings
  src/components/        dot · chat panel · approval card · plan view · screen selector · toast · switch
  src-tauri/src/         windows · state · capture · hotkeys · tray
skills/                  clean_downloads · research_topic · weekly_report
                         second_brain · autonomous_goal · decision_journal
scripts/                 start-dobot.bat — backend + desktop app in one double-click
docs/                    architecture · security · memory · agent · development · install
report.md               development and verification report (V1 → V2, test evidence, gaps)
examples/                runnable end-to-end flows
```

## Documentation

| Document | Contents |
| --- | --- |
| [`docs/install.md`](docs/install.md) | Build, package and install Dobot on your own laptop |
| [`docs/architecture.md`](docs/architecture.md) | Every module, the request lifecycle, the degradation model |
| [`docs/security.md`](docs/security.md) | Risk model, policies, approvals, execution modes, file guard, evasion detection, skill scanner, kill switch, what is not enforced |
| [`docs/memory.md`](docs/memory.md) | Memory types, recall scoring, stores and vectors |
| [`docs/agent.md`](docs/agent.md) | Hermes runtimes, toolsets, skills and workflow authoring |
| [`docs/development.md`](docs/development.md) | Setup, phase mapping, demo script, conventions |
| [`report.md`](report.md) | Development and verification report: what V1 shipped, what V2 changed, how each claim was tested, what is still unproven |

---

## Status

Everything in the MVP specification's phases 1–11 is implemented and verified: desktop shell, backend
gateway, Nemotron planning, screen context, Tavily research, agent execution, the NemoClaw boundary,
the decision engine, memory, verification and polish. The desktop app is confirmed to build into real
Windows installers — see [`docs/install.md`](docs/install.md).

**What is genuinely not there yet** — stated plainly, because a false capability is worse than a
missing one:

| Gap | Reality |
| --- | --- |
| OS-level sandbox | On Windows the default is `SANDBOX_PROVIDER=local`, so `isolation: none`. The action firewall is enforced in-process; NemoClaw/OpenShell must be running for kernel-level isolation. |
| Desktop (GUI) control | The `computer_*` tools need the Hermes runtime. Without it they refuse rather than pretend to click. |
| Outbound messaging / email | `message_send` composes a draft and reports `sent: false`. Dobot does not send anything. |
| Voice | Both halves work but need local pieces: speaking uses the OS engine (PowerShell SAPI) and listening downloads a ~484 MB Whisper-small checkpoint (`uv sync --extra voice`, plus ffmpeg). Neither is in the container image, so a backend running in Docker reports both as declined. |
| API authentication | Loopback runs open by default (single-user laptop). A deployed backend must set `DOBOT_API_TOKEN`; on a private Hugging Face Space, that gate and the Space proxy's token check both sit in front of every request. |
| Long-term memory backends | Defaults to a local file store with a local cosine index; MongoDB and Zilliz are optional extras. |
| Automatic skill learning | Skills are authored in `skills/` or saved explicitly. Dobot does not invent workflows on its own. |
| Mobile, email-ecosystem and financial automation | Explicitly future work. |
| Backend autostart | The desktop app spawns no backend by default — it uses whatever **Settings → Backend** points at (`127.0.0.1:8756` unless you change it). Start it yourself, or build the opt-in sidecar installer; `docs/install.md` §5 covers the options. |

One specification conflict is resolved deliberately: §58 mentions Postgres + pgvector, while the
product header specifies MongoDB + Zilliz. The header wins, and it is recorded in
[`docs/memory.md`](docs/memory.md). The store interfaces are narrow enough to add a third backend
without touching the orchestrator.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
