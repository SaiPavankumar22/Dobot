# Development

## Prerequisites

- Python 3.11+
- Node 20+ (Node 26 verified)
- Rust 1.77+ with cargo (for the Tauri shell)
- Optional: MongoDB, Zilliz, Hermes CLI, NemoClaw CLI, tesseract

Nothing above is required to run Dobot — see the degradation model in `docs/architecture.md`.

## Backend

```bash
cd backend
uv venv
uv pip install -e ".[dev]"        # add ".[dev,mongo,vectors,ocr]" for the optional stores
uv run uvicorn app.main:app --host 127.0.0.1 --port 8756 --reload
```

- `GET /health` — liveness plus which providers are live
- `GET /docs` — OpenAPI
- `GET /settings/providers` — what the UI shows about connection status
- `ws://127.0.0.1:8756/ws` — event stream (recent events are replayed on connect)

Useful checks:

```bash
uv run pytest -q                       # unit + integration tests
uv run python -m app.selftest          # end-to-end loop against whatever providers are configured
```

## Desktop

```bash
cd desktop
npm install
npm run dev            # browser UI, talks to the backend on :8756
npm run tauri dev      # real shell: chat window, tray, global hotkeys, screen capture, the dot
npm run build          # tsc + vite production build
cargo check --manifest-path src-tauri/Cargo.toml
```

The UI degrades gracefully outside Tauri: screen selection uses the backend's capture path and
native-only controls are hidden, so `npm run dev` is a fully usable development loop.

### Windows, and the two modes

One bundle renders five Tauri windows, chosen by window label (or, in the browser, by hash):

| Window | Default | Purpose |
| --- | --- | --- |
| `chat` | visible | The ordinary chatbot surface — sidebar, thread, composer. What a fresh install opens. |
| `dot` | **hidden** | The always-on floating dot. Shown only when the user turns **Always on** on. |
| `panel` | hidden | Compact quick-ask surface, anchored to the dot. |
| `dashboard` | hidden | Tasks, automations, approvals, memory, skills, activity, security, settings. |
| `overlay` | created on demand | Temporary transparent region-selection layer. |

The always-on choice and the dot's position persist in `%APPDATA%\com.dobot.desktop\dobot-state.json`
(see `src-tauri/src/state.rs`). `windows::apply_startup_state` reads that at launch; the tray menu, the
chat header, Security and Settings all drive the same `set_dot_enabled` command, and the shell echoes
the result back to every window over the `dobot://dot-enabled` event so the switches cannot drift.

Any change here needs both halves rebuilt: `npm run build` for the UI, `cargo check` for the shell.

Building a distributable installer is covered in [`install.md`](install.md).

## Phase mapping (specification §84–94)

| Phase | Delivered in |
| --- | --- |
| 1 Desktop shell | `desktop/src-tauri/*`, `desktop/src/components/DobotDot.tsx` |
| 2 Backend | `backend/app/main.py`, `backend/app/api/*`, `backend/app/events.py` |
| 3 Nebius | `backend/app/agents/nemotron.py`, `backend/app/core/router.py` |
| 4 Screen context | `desktop/src/components/ScreenSelector.tsx`, `backend/app/tools/screen.py`, `backend/app/core/context_engine.py` |
| 5 Tavily | `backend/app/tools/tavily.py`, `backend/app/agents/research_agent.py` |
| 6 Hermes | `backend/app/agents/hermes.py`, `backend/app/tools/*` |
| 7 NemoClaw | `backend/app/agents/sandbox.py` |
| 8 Decision engine | `backend/app/core/decision_engine.py`, `backend/app/security/*` |
| 9 Memory | `backend/app/memory/*` |
| 10 Verification | `backend/app/core/verifier.py` |
| 11 Polish | `desktop/src/pages/*` (chat-first UI), `desktop/src/components/*`, notifications, onboarding |

## Demo script

1. Launch Dobot; the **chat window** opens with nothing floating. Turn on **Always on** in the header
   and the dot appears above every window.
2. Open a research PDF, select a paragraph, ask *"Explain this in simple terms."*
3. Ask *"Find recent research about this."* — research events stream, sources are listed.
4. Ask *"Remember that this project uses Zilliz for long-term memory."* then later ask what database
   Dobot uses for memory.
5. Ask *"Open my Dobot project in VS Code."* — an `app_open` action runs, verification confirms.
6. Ask *"Clean my Downloads folder."* — Shadow Mode or an approval dialog shows 32 screenshots / 14
   PDFs / 8 installers / 4 unknown files, with the unknown files excluded from the auto-approvable set.
7. Ask *"Create a TODO.md in my project with the implementation plan."* — creation plus existence,
   readability and content verification.
8. Create *"Every Friday at 6 PM, research new AI agent releases and send me a summary."* — it appears
   under Automations with the next run time.
9. Press the kill switch mid-execution to show immediate cancellation.

## Conventions

- Python: `ruff`-clean style, typed, async-first, no blocking I/O on the event loop.
- One interface per external dependency, with a working local fallback.
- Deterministic policy code stays pure and unit-testable; the model never decides safety on its own.
- Secrets live in `.env` only. Log lines redact known secret values.
