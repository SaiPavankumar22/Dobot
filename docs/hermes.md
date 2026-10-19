# Hermes: the real agent behind Dobot

Dobot does not ship an agent of its own for desktop work — it orchestrates **Hermes Agent**
(`NousResearch/hermes-agent`, MIT). Until recently the code in `backend/app/agents/hermes.py` only
*pretended* to: it defined a runtime protocol with a CLI mode and a "remote" mode that POSTed to
`/run`, `/computer` and `/browser/submit`. Those routes do not exist in Hermes. A gateway was
reachable, Dobot still could not do anything, because every call 404'd.

This page records what the real integration is, how to stand it up, and how it was verified.

---

## 1. What Hermes actually is

Hermes is a self-hostable agent product, not a library:

| Surface | Purpose |
| --- | --- |
| `hermes` CLI / TUI | Interactive chat, `hermes model`, `hermes tools`, `hermes computer-use` |
| `hermes gateway` | Long-running process: messaging platforms **and** the HTTP API server |
| `AIAgent` (`run_agent.py`) | In-process Python agent, the same core the gateway uses |
| Toolsets | `computer_use`, `browser`, `terminal`, `file`, `web`, `skills`, `memory`, `delegation`, … |
| `cua-driver` | Desktop control backend (UIAutomation / Accessibility, background input) |

The API server is OpenAI-compatible and is enabled with `API_SERVER_KEY` in the Hermes home `.env`
(on Windows: `%LOCALAPPDATA%\hermes\.env`). It binds `127.0.0.1:8642` by default.

### The contract Dobot uses

Taken from `gateway/platforms/api_server.py` and `api_server_runs.py`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/health` | liveness |
| `GET` | `/v1/models`, `/v1/capabilities`, `/v1/toolsets` | discovery |
| `POST` | `/v1/runs` `{"input", "session_id"}` | start one agent turn → `{"run_id"}` |
| `GET` | `/v1/runs/{id}/events` | SSE: `tool.started`, `tool.completed`, `run.completed`, `run.failed` |
| `GET` | `/v1/runs/{id}` | pollable status (`output`, `error`, `usage`) |
| `POST` | `/v1/runs/{id}/stop` | cancellation |
| `POST` | `/v1/chat/completions` | plain OpenAI fallback for older builds |

Auth on every route: `Authorization: Bearer $API_SERVER_KEY`.

---

## 2. How Dobot integrates

`backend/app/agents/hermes.py` → `HermesApiRuntime`:

1. `info()` probes `/v1/health` (2.5 s) so the UI can say **available / unreachable / key rejected**
   instead of guessing from a configured URL.
2. `execute(prompt)` creates a run, consumes the SSE stream, and returns
   `{ok, status, output, error, run_id, tools[]}` — the tool trace is the evidence the Verifier reads.
3. Timeouts stop the run server-side (`/v1/runs/{id}/stop`) rather than abandoning it.
4. If `/v1/runs` is missing (older gateway), it falls back to `/v1/chat/completions` and says so.
5. `401` is reported as a key mismatch, never swallowed.

`AGENT_RUNTIME` values:

| Value | Runtime |
| --- | --- |
| `local` (default) | Dobot's own guarded tools; desktop control reports unavailable |
| `cli` | the `hermes` CLI with a toolset allowlist |
| `remote` / `api` / `hermes` | `HermesApiRuntime` (recommended) |
| `auto` | the API runtime |

`hermes_run` (`backend/app/tools/hermes_task.py`) is the generic door: the planner hands a whole
desktop job ("open report.xlsx, chart column B, save it") to Hermes instead of hand-writing a chain
of `computer_*` gestures. Single known gestures still use `computer_click` / `computer_type` / …

---

## 3. Standing it up (Windows)

```bash
# 1. get the source and its environment
git clone --depth 1 https://github.com/NousResearch/hermes-agent D:/All/hermes-agent
cd D:/All/hermes-agent && uv sync --no-dev

# 2. the API server needs aiohttp in the *managed* generation venv Hermes adopts at runtime
uv pip install --python "<...>/installs/<id>/environments/<gen>/venv/Scripts/python.exe" aiohttp
#    (the generation path is printed by: python -c "import sys; print(sys.path)")

# 3. enable the API server + give it a key and a model
#    %LOCALAPPDATA%\hermes\.env
#      API_SERVER_ENABLED=true
#      API_SERVER_KEY=<32-byte secret>
#      API_SERVER_PORT=8642
#      NEBIUS_API_KEY=<provider key>
#    %LOCALAPPDATA%\hermes\config.yaml
#      model: {default: nvidia/nemotron-3-super-120b-a12b, provider: custom,
#              base_url: https://api.tokenfactory.nebius.com/v1, key_env: NEBIUS_API_KEY}

# 4. desktop control
hermes computer-use install      # cua-driver, pinned
hermes tools enable computer_use

# 5. run it
hermes gateway run               # or: hermes gateway install  (autostart)
```

Then point Dobot at it:

```dotenv
AGENT_RUNTIME=remote
HERMES_ENDPOINT=http://127.0.0.1:8642
HERMES_API_KEY=<the same API_SERVER_KEY>
```

Check it from Dobot: `GET /settings/providers` → `agent_runtime: "hermes-agent:available"`.

> **Model note.** Hermes runs its own model loop. Without a provider key it starts, serves
> `/v1/health`, and fails runs with `Hermes is not connected to any AI provider yet`. That is the
> honest state to be in: the integration is live, the agent has nothing to think with.

---

## 4. Two servers in one deployment (container)

A container is one box, but nothing stops two servers from living in it: the backend on `:8756`
and the Hermes gateway sidecar on `127.0.0.1:8642`. The container entrypoint starts the
gateway **in the background** and `exec`s the backend immediately — the backend never waits on
Hermes, so the container is up and serving chat, files, terminal, browser and research whether the
gateway is booting, crashed, or deliberately off.

| Secret / variable | Needed? | What it does |
| --- | --- | --- |
| `HERMES_SIDECAR` | optional (default `1`) | `0` runs the container with the backend only — Dobot degrades to its own tools |
| `NEBIUS_API_KEY` | required for real runs | The gateway runs its **own** model loop; it reads this from its own env, never the keys a user pastes into the app |
| `API_SERVER_KEY` | optional | The shared bearer key. On first boot the entrypoint generates one and writes it to `HERMES_HOME/.env`; both processes then present the same key |
| `AGENT_RUNTIME` | default `auto` (baked into the image) | Probes the sidecar on a TTL (`HERMES_HEALTH_TTL_SECONDS`, 30 s) and serves on local tools when it is down |

What runs where:

```
host proxy ──:8756──▶ Dobot backend (uvicorn, AGENT_RUNTIME=auto)
                                │  probe /v1/health (TTL 30 s)
                                └──127.0.0.1:8642──▶ hermes gateway run  (sidecar, loopback only)
                                                        └─ cua-driver / toolsets
```

Two properties are worth stating plainly:

* **The gateway is loopback-only.** The platform routes only the declared app port, so `/v1/runs`
  never faces the internet; the only public surface is the backend behind the proxy + token gate.
* **Degradation is tested, not hoped for.** With the gateway down, `ResilientRuntime` reports
  `dobot-local-tools`, the planner drops `hermes_run`/`computer_*` steps instead of planning
  gestures that cannot run (and tells the model why), and chat/files/terminal/browser/research
  answer normally. `hermes_fallback_local=false` is the strict "Hermes or nothing" switch.

### If the container must control **your** desktop

The sidecar's `cua-driver` controls the *container*, not your PC — a cloud Linux box has no
accessible desktop of yours. For real desktop control run the gateway on the machine that has the
display (§3) and point the backend at it over a tunnel instead of the sidecar:

```bash
# on your machine, next to the gateway from §3
cloudflared tunnel --url http://127.0.0.1:8642      # or ngrok http 8642
```

```dotenv
# container environment
HERMES_SIDECAR=0
AGENT_RUNTIME=auto
HERMES_ENDPOINT=https://<tunnel-url>
HERMES_API_KEY=<the same API_SERVER_KEY>
```

Then a single task can genuinely span both servers: the planner in the container decides, the
gateway on your PC does the GUI work, and the tool trace comes back over SSE for the Verifier.
When the tunnel or the PC is off, the same degradation path applies — Dobot keeps working and
names the part that is blocked.

---

## 5. What was verified (not claimed)

Against the live gateway on this machine:

| Check | Result |
| --- | --- |
| `GET /v1/health` with bearer auth | `200 {"status": "ok"}` |
| `POST /v1/runs` → `GET /v1/runs/{id}/events` | run created, `run.completed`, final `output` returned |
| Tool trace surfaced to Dobot | `tool.started` + `tool.completed` for `browser_exec`, then `computer_use` |
| Real desktop control | `computer_use {action: list_apps}` returned the machine's running apps (Freebuff active, Chrome running, cua-driver) |
| `cua-driver doctor` | `ax_capability ✅ UIAutomation reachable`, `screen_capture_capability ✅` |
| Bad key | reported as `401` by the adapter, not swallowed |
| No model provider | run `failed` with the provider message, status surfaced |

The stream test used a local OpenAI-compatible stub model (`tool` mode) to drive the agent loop
offline; the desktop enumeration itself is real.

Automated coverage: `backend/tests/test_hermes_runtime.py` (18 tests — including strict vs.
resilient `build_runtime` wiring) plus planner gating tests (`hermes_run`/`computer_*` dropped
when the probe fails), in the suite at 370+ passing. The two-server container in §4 was built and
exercised with Docker: both servers up together, then the sidecar off → backend still healthy.

---

## 6. On REA

[`morluto/rea`](https://github.com/morluto/rea) is an MCP toolkit for reverse engineering **closed**
targets: native binaries, Electron/ASAR bundles, .NET, APKs, firmware, packet captures. It is the
right instrument when you only have the artifact.

Hermes is MIT-licensed Python. The source *is* the ground truth, and it is cheaper and more accurate
to read `api_server.py` than to infer the HTTP contract from traffic. Running REA here would have
produced a weaker version of a fact that was already public — so the integration above is built from
source plus a live probe, and every claim in §4 was executed.

Where REA would genuinely pay off: when the target is something Dobot must automate but cannot read —
a closed Windows app (Electron/MSIX), an Android app, or a vendor binary. In that case the flow is:

```bash
npx -y rea-agents@latest analyze-javascript-application "C:/path/to/app" --json   # Electron
npx -y rea-agents@latest setup                                                    # register the MCP server
```

and then ask the agent to explain the feature and rebuild it inside Dobot.
