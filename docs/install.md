# Installing Dobot on your laptop

This is the practical guide: how to go from the source checkout to something you double-click.

Dobot is **two processes**:

| Process | What it is | What it does |
| --- | --- | --- |
| **Backend** | FastAPI app (Python) on `127.0.0.1:8756` | All reasoning, memory, tools and safety decisions |
| **Desktop app** | Tauri shell + web UI (`Dobot.exe`) | The chat window, the floating dot, hotkeys, screen capture |

The desktop app is the part you install. The backend has to be running for Dobot to think, so
there are exactly three ways to run the pair — choose yours in
[§0](#0-the-three-ways-to-run-dobot):

1. **From source, both local** — desktop and backend, each in its own terminal, from the checkout
   (development and for reading the code).
2. **The `.exe`, backend local** — the installed app, with the backend still in a terminal on the
   same machine.
3. **The `.exe`, backend hosted** — the installed app pointed at a backend you host somewhere
   else. No terminal at all after it is set up.

The installer never ships a backend *inside* the exe (that experiment crashed the laptop it was
first tried on); bundling one is an opt-in flag, see [§4](#4-ways-2-and-3--build-a-real-installer). A
single backend that several devices share is a server-shaped variant of way 3:
[§5.5](#55-deploy-one-backend-point-every-device-at-it).

---

## 0. The three ways to run Dobot

Same two processes, three arrangements. Everything after this section is detail for one of them —
the numbered sections work the same either way.

| | **1 · From source, both local** | **2 · The `.exe`, backend local** | **3 · The `.exe`, backend hosted** |
| --- | --- | --- | --- |
| Desktop | `npm run tauri dev` in the checkout | installed `Dobot.exe` | installed `Dobot.exe` |
| Backend | `uvicorn` in a terminal, same machine | `uvicorn` in a terminal, same machine | a backend you host — your own server, container, or Space |
| Backend URL | `http://127.0.0.1:8756` (the dev default) | `http://127.0.0.1:8756` (the default) | whatever URL you host it at (you set it in Settings) |
| Terminal needed | two, while Dobot runs | one, while Dobot runs | none |
| Source checkout needed | yes | only to build | only to build |
| What `fs_*` / `computer_*` act on | this laptop | this laptop | that backend's machine |
| Voice input (mic) | local Whisper | local Whisper | only if that backend has the model — the Doctor page says so |
| Good for | development, reading the code | daily use on one machine | weak hardware, a second laptop, demos |

The **Settings → Backend** URL is the single switch between ways 2 and 3: it is stored per machine,
so changing it never touches the other one.

### Way 1 — desktop and backend, both from source

Do [§1](#1-check-the-prerequisites) (prerequisites) and [§2](#2-configure-the-backend-once)
(`.env` + dependencies) once, then keep **two terminals** open:

```bash
# terminal 1 — the backend
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8756

# terminal 2 — the desktop app
cd desktop
npm install
npm run tauri dev
```

Nothing to configure: a dev build already points at `http://127.0.0.1:8756`. Check the brain is up
before blaming the app:

```bash
curl http://127.0.0.1:8756/health
```

Stop either one with `Ctrl+C` — Dobot only thinks while terminal 1 is alive. For a UI-only loop with
no native shell (no tray, no global hotkeys, screen selection falls back to display-share) use
`npm run dev` and open <http://127.0.0.1:1420>. [§3](#3-way-1--run-it-from-source-fastest-no-installer)
is this same setup as its own section.

### Way 2 — the installed `.exe`, backend still on this machine

**Build once** ([§4](#4-ways-2-and-3--build-a-real-installer)) and install what it produces:

```bash
cd desktop
npm install
npm run tauri build
# then double-click:
#   desktop/src-tauri/target/release/bundle/nsis/Dobot_0.1.0_x64-setup.exe
```

**Then every day** — one terminal for the brain:

```bash
cd path/to/Dobot/backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8756
```

Launch **Dobot** from the Start menu — every build starts pointed at `http://127.0.0.1:8756`, so
usually nothing needs changing. This is the short checklist anyway:

1. **Settings → Backend** should read `http://127.0.0.1:8756`. Only if you moved the backend to
   another port, paste that URL and **Save and reconnect**.
2. Leave the token field empty and press **Save token** — that clears a token saved earlier for
   way 3. A local backend with no `DOBOT_API_TOKEN` does not ask for one (the note under the field
   says *the connected backend is not asking for a token right now*), and **Test connection**
   answers ✓. If you do gate it, generate one with
   `uv run python -m app.selftest --new-token` and paste it here.
3. **Settings → Your API keys** → paste `nebius` (required) and `tavily` → **Save**. They are
   written to *that* backend's `.env` and take effect immediately.
4. The sidebar footer chip reads **backend connected**; the Doctor page lists what is live.

You do not need the checkout on this machine once the installer is built — only the backend's
folder (or its `.env`) and the installed app.

### Way 3 — the installed `.exe`, backend hosted somewhere else

The backend runs where you do not run a terminal: your own server ([§5.5](#55-deploy-one-backend-point-every-device-at-it)),
your own container, or your own Hugging Face Space ([§5.6](#56-deploy-the-backend-to-a-hugging-face-space)).
Get that backend up first — one command from the repo root pushes a Space:

```bash
HF_TOKEN=hf_xxx HF_SPACE=YOUR_SPACE bash scripts/deploy-hf-space.sh
```

Then, on the device:

1. **Install and launch** the `.exe`. It starts pointed at this machine
   (`http://127.0.0.1:8756`).
2. **Settings → Backend** → the URL of your hosted backend (for a Space:
   `https://YOUR_SPACE.hf.space`) → **Save and reconnect**.
3. If that backend guards itself (`DOBOT_API_TOKEN`) — and a hosted one should — paste the token in
   the token field on the same page → **Save token** → **Test connection**; the note turns into
   `✓ Connected — backend ok`. For a private Space the token is a **read** token from
   <https://huggingface.co/settings/tokens>: without it the Space's proxy answers **404** to
   everything, which is the private Space behaving correctly, not a broken app (the field's own
   note will read *this backend requires a token*).
4. **Settings → Your API keys** → paste `nebius` (required) and `tavily` → **Save**. They land in
   *that* backend's `.env`; `mongodb_uri` and the LangSmith values show read-only under *Managed for
   you*.
5. Wait out the first wake (a sleeping free backend can take ~30 s) until the sidebar chip reads
   **backend connected**, then confirm on the Doctor page.

What you are accepting, honestly: `fs_*` and `computer_*` act on **that backend's machine**, not
your laptop; screen selections and mic audio travel to it; a container's filesystem is usually
wiped on rebuild (set `MONGODB_URI` as a secret if memory must survive); and everyone pointed at
one backend shares one `.env` — the last save wins.

### Which way am I in?

**Settings → Backend** says it outright: `http://127.0.0.1:8756` is way 1 or 2 (way 1 is the one
running from a checkout, `npm run tauri dev`), any other URL is way 3. The sidebar footer chip and
the Doctor page then tell you whether that backend actually answered.

---

## 1. Check the prerequisites

```bash
python --version      # 3.11 or newer
uv --version          # https://docs.astral.sh/uv/
node --version        # 20 or newer
npm --version
rustc --version       # 1.77 or newer
cargo --version
```

Windows also needs the **Microsoft C++ Build Tools** (the MSVC toolchain) and **WebView2**. WebView2
ships with Windows 11 and current Windows 10 updates; the installer will tell you if it is missing.
To confirm everything is present before you spend time on a build:

```bash
cd desktop
npx tauri info
```

You want to see a ✔ next to `WebView2`, `MSVC`, `rustc` and `cargo`. On the machine this guide was
written against that was Windows 10.0.26200 (x64), WebView2 153, Visual Studio Community 2022, rustc
and cargo 1.97.1, Node 26.5.0 and npm 11.17.0.

---

## 2. Configure the backend once

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```ini
NEBIUS_API_KEY=...        # reasoning (required for real answers)
TAVILY_API_KEY=...        # web research
```

Optionally add `MONGODB_URI` and `ZILLIZ_URI` for durable memory. Leave them blank and Dobot uses its
local file store and local vector index instead — it still works, it just keeps memory on disk.

`.env` is gitignored. Never commit it, and never put a key anywhere except this file.

Then install the backend's dependencies:

```bash
cd backend
uv venv
uv pip install -e ".[dev]"            # add ",mongo,vectors,ocr" for the optional stores
```

Verify:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8756
# in another terminal:
curl http://127.0.0.1:8756/health
```

`"degraded":[]` means every configured provider answered. `"degraded":["tavily"]` means Dobot is
running fine but research is offline — that is a normal, supported state.

### Optional: turn on Laya (model-backed risk judgement)

By default the decision engine uses deterministic heuristics only. To add **Laya** — the open
System-1 judgement model behind JEV — pick one of:

```bash
# Option A: a small sidecar server (recommended)
pip install "laya[serve]"
laya-serve                          # listens on 127.0.0.1:8000
# then add to .env:  LAYA_SERVER_URL=http://127.0.0.1:8000

# Option B: in-process with the backend
pip install laya
# then add to .env:  LAYA_INPROCESS=1
```

With neither, Dobot still runs — JEV just uses its built-in heuristics. Check which mode is live via
the Doctor page in the app or `curl http://127.0.0.1:8756/doctor`.

---

## 3. Way 1 — run it from source (fastest, no installer)

Good for trying it or developing. Open **two** terminals.

Terminal 1, the backend:

```bash
cd backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8756
```

Terminal 2, the desktop app:

```bash
cd desktop
npm install
npm run tauri dev
```

Dobot opens as a chat window. Nothing floats until you turn on **Always on** in the header.

For a UI-only loop (no native shell — no tray, no global hotkeys, screen selection falls back to the
browser's display-share permission) use `npm run dev` and open <http://127.0.0.1:1420>.

---

## 4. Ways 2 and 3 — build a real installer

This produces an actual Windows installer you can install, pin and uninstall like any other app.

```bash
cd desktop
npm install
npm run tauri build
```

That runs `tsc --noEmit && vite build`, compiles the Rust shell in release mode, and then bundles
installers. `tauri.conf.json` sets `"targets": "all"`, which on Windows means **both** an MSI and an
NSIS setup executable:

```
desktop/src-tauri/target/release/bundle/msi/Dobot_0.1.0_x64_en-US.msi       (~3.3 MB)
desktop/src-tauri/target/release/bundle/nsis/Dobot_0.1.0_x64-setup.exe      (~2.3 MB)
desktop/src-tauri/target/release/dobot-desktop.exe                          # the shell

(Those sizes are small on purpose: no backend rides along — see "Where the backend comes from"
below. The same build with `DOBOT_BUNDLE_SIDECAR=1` is ~50 MB.)
```

Pick either: the **NSIS `.exe`** is the friendlier one (per-user install, no admin prompt), the
**MSI** is better if you manage machines with policy.

### Where the backend comes from

Since the shell and the brain are two processes, the installed app needs a brain somewhere — and it
never ships *inside* the installer: a PyInstaller backend bundled into the exe crashed the laptop it
was first tried on. The installed app connects to a backend in this order:

1. **The backend you point it at** — **Settings → Backend** stores a URL (plus the token every call
   carries) per machine. Every build defaults to `http://127.0.0.1:8756`; a backend hosted anywhere
   else — §5.5 or §5.6 — is a URL pasted there.
2. **A backend running on this machine** — the app never searches for one: set **Settings →
   Backend** to `http://127.0.0.1:8756` and it uses that instead ([way 2](#way-2--the-installed-exe-backend-still-on-this-machine);
   you start the backend yourself, §5.1–5.4).

On an installed machine, `%LOCALAPPDATA%\Dobot\` contains the shell and the writable `skills/`
folder the Skills page manages.

**Opt-in: bundle the backend after all.** The sidecar machinery is still there — it is just no
longer the default. Build the PyInstaller exe first, then build the installer with the flag:

```bash
npm run sidecar            # or scripts\build-backend-exe.bat from the repo root (~1–2 min)
set DOBOT_BUNDLE_SIDECAR=1&& npm run tauri build
```

That installer starts the backend on launch (waiting for its port before the UI connects), stops it
when you quit Dobot (tray → Quit, or closing the chat window with the dot off), **adopts** an
already-running backend instead of spawning a second one, and does none of this in
`npm run tauri dev`. The build fails loudly if the sidecar exe is missing, so a bundle with a hole
cannot happen by accident. Two honest notes: the PyInstaller exe unpacks itself on each launch (a
few extra seconds on first paint), and a bundled backend still dies with the machine it runs on —
exactly why a deployed one is the default.

Notes worth knowing before you run it:

- **The first release build is slow.** `[profile.release]` uses `lto = true, codegen-units = 1,
  opt-level = "s"` and the Tauri dependency tree is large — expect several minutes, and expect that
  figure to be dominated by compiling dependencies you will not pay for again.
- **The bundler downloads WiX and NSIS on first use.** Confirmed: `tauri build` fetched `wix311`,
  `nsis-3.11` and `nsis_tauri_utils-v0.5.3` into the Tauri cache, validated their hashes, and then ran
  `candle`/`light` (MSI) and `makensis` (NSIS). It is a one-off and it needs network access the first
  time; after that the build is offline.
- **Skip the long build while iterating** with a debug bundle: `npx tauri build --debug`. Same
  installers, debug binary, far faster — fine for confirming the pipeline works. It writes to the same
  layout under `target/debug/`, and it is the path this project's bundle was tested on:

  ```
  desktop/src-tauri/target/debug/bundle/msi/Dobot_0.1.0_x64_en-US.msi      (5.1 MB)
  desktop/src-tauri/target/debug/bundle/nsis/Dobot_0.1.0_x64-setup.exe     (3.1 MB)
  desktop/src-tauri/target/debug/dobot-desktop.exe                         (raw binary, 16.7 MB)
  ```

You can change what gets produced in `desktop/src-tauri/tauri.conf.json`:

```json
"bundle": {
  "targets": ["nsis"]          // or ["msi"], or ["nsis", "msi"], or "all"
}
```

### Installing what you built

Double-click `Dobot_0.1.0_x64-setup.exe` (or the `.msi`) and follow the prompts. Windows SmartScreen
will warn you because the installer is unsigned — **More info → Run anyway**. Code signing is a
purchase and a certificate, not a code change.

After installing you get a **Dobot** entry in the Start menu and, if you install with the NSIS bundle,
a desktop shortcut.

### First run (on any device)

1. Launch **Dobot**. The **chat window** opens — sidebar, welcome screen, composer. No dot.
2. Turn on **Always on** in the header if you want the floating dot.
3. Open **Settings → Your API keys** and paste your `nebius` key (and `tavily`, optionally). They
   are persisted to the connected backend's `.env` and applied immediately — no restart, no
   terminal.
4. The sidebar footer chip reads *backend connected* a few seconds after launch. If a device will
   use a shared server instead, see §5.5.

### Adding API keys after packaging (no terminal needed)

A packaged install does not assume you will ever open a terminal or edit a file by hand. In the app:

**Settings → Your API keys** — paste a key, press **Save**. It is persisted to the backend's `.env`
and applied to the running process immediately (no restart). The field only ever shows a masked hint
like `sk-1…ab12 (48 chars)`, never the key itself; **Clear** blanks it.

The page asks for exactly four credentials, all of them yours:

| Key | Why |
| --- | --- |
| `nebius` | **Required.** Every Nemotron model: reasoning, the vision model that reads attachments, screen analysis. Get one at studio.nebius.com |
| `tavily` | Web research and the news automations |
| `zilliz_token` | Vector memory — recall across past conversations and documents (optional) |
| `zilliz_uri` | The cluster endpoint that token belongs to (optional, needed with `zilliz_token`) |

Everything else the backend needs is **infrastructure**, not a user credential, and the app never
asks for it. Those values are read-only on the page, listed with where they come from:

| Value | Who sets it |
| --- | --- |
| `mongodb_uri` | whoever runs the backend — its `.env`, or a Space secret when it is hosted for you |
| `langsmith_api_key`, `langsmith_api_url`, `langsmith_project` | same, for usage tracing |
| `dobot_api_token` | same — the backend's own access gate |
| `laya_api_key`, `laya_server_url` | same, for the optional Laya judgement server |

`PUT`/`DELETE` of those names returns **403 `OPERATOR_MANAGED`**, so a stale client cannot quietly
overwrite the operator's configuration either. To change them, edit `.env` (or the Space secrets) and
restart the backend.

> **Which backend are your keys stored on?** Whichever one Settings → Backend points at. Point the
> app at your own backend — a local one (§5.1–5.4) or your own copy of the Space (§5.6) — and the
> four keys are yours alone. Point every device at one shared backend and you are sharing one `.env`.

If you prefer the terminal anyway, editing `.env` in the repo root and restarting the backend does
exactly the same thing — see [`prerequisites.md`](prerequisites.md) for every variable.

### Optional: voice input (mic button in chat and on the widget)

The mic button transcribes speech **locally** with Whisper via `faster-whisper` (audio never leaves
the machine; the checkpoint is downloaded once, on first use). To enable it:

```bash
cd backend && uv sync --all-extras
```

`voice` is the extra you need (`faster-whisper`); `--all-extras` is the safe form because **a plain
`uv sync` removes anything not asked for**, including the `dev` extra's pytest and ruff. If you would
rather be surgical: `uv sync --extra voice --extra dev`.

**On a laptop with an NVIDIA card, add the GPU runtime too** — it is the difference between
faster-than-realtime and unusable:

```bash
cd backend && uv sync --all-extras --extra voice-gpu
```

`voice-gpu` adds `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` (Windows). ctranslate2 loads
`cublas64_12.dll`/`cudnn64_9.dll` by bare name, so without a CUDA toolkit installed the GPU attempt
dies with `Library cublas64_12.dll is not found` and every recording falls back to the CPU — measured
on a 4 GB RTX 3050: **2.4 s with the GPU vs 40 s on the CPU** for a 5.7 s clip. Dobot puts those
wheel directories on `PATH` for itself, so no system configuration is involved.

**The default checkpoint is Whisper *small*** (`Systran/faster-whisper-small`: 244M parameters,
MIT, ~484 MB on disk, ~250 MB of RAM at int8, loads natively as a CTranslate2 build with no
conversion). It was chosen over the small CrisperWhisper 2.0 checkpoint deliberately: same size
class and compute, but MIT-licensed and loadable by `faster-whisper` as published.

**CrisperWhisper stays available** — its verbatim `[UM]`/`[UH]` output is nice when you want it.
Its checkpoints **must be CT2 builds**: `nyralabs/faster_CrisperWhisper` (~3 GB, CrisperWhisper 1.0,
verbatim with fillers) works as-is; the `nyralabs/CrisperWhisper2.0_*` repos ship transformers
safetensors for a custom architecture and **cannot** be loaded by `faster-whisper` (you get
`Unable to open file 'model.bin'`) — convert one first (`pip install "crisperwhisper[convert]"`)
and pass the resulting directory. Point `TRANSCRIBER_MODEL` at whichever CT2 repo you want.

Also install [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) and put it on PATH — browser and WebView
recordings arrive as webm/ogg, which the model needs ffmpeg to decode. Check the **Doctor** page:
it reports exactly what is missing, which device the model loaded on, and why it moved off the
preferred one. Without it, the mic button explains why instead of silently failing.

`TRANSCRIBER_DEVICE=auto` prefers the GPU and demotes itself to the CPU if the card cannot load the
checkpoint (the GPU attempt just fails to load or run and the CPU carries on) or cannot run it (the
missing-DLL case above). An explicit `TRANSCRIBER_DEVICE=cuda` is honoured literally instead, so a
setting that says cuda never quietly means something else.

### Attaching images and files in chat

Clip a file onto the composer — the paperclip, a drag-and-drop onto the chat, or a paste — and Dobot
reads it with the answer:

| Kind | Accepted | Limits |
| --- | --- | --- |
| Images | png, jpg/jpeg, webp, gif | 4 per message, 5 MB each |
| Text | txt, md, markdown, csv, tsv, json, log, yaml/yml, py, ts, js, tsx, jsx, sh, sql, html, css, toml, ini, cfg | 4 per message, 256 KB each |

Images go to the **vision model** (`NEMOTRON_VISION_MODEL`, default `openbmb/MiniCPM-V-4_5` on the same
Nebius account as the reasoning models) and are described into the conversation before the planner
runs, so a screenshot can be reasoned about, remembered and searched like any other text. Text files
are decoded and injected as a fenced block, clipped to ~4 000 characters per file and ~6 000 per
message so one log file cannot swallow the context window. The whole request may be 24 MB.

The composer enforces those numbers *before* uploading — same constants, published by the backend at
`GET /chat/attachments`, so the two can never drift. A file over the limit is rejected with the
exact reason (`screenshot.png is 6.2 MB — the limit is 5 MB per image`) rather than failing silently
on submit. PDFs
are deliberately *not* accepted: decoding them needs `pypdf`, and a PDF that gets mis-decoded is worse
than one that is refused.

A message with only an attachment is valid — drop an image in and press send to ask "what is this?".

### Optional: deep research and the LangGraph harness

Both ride on `langgraph`, which is already in the backend's dependencies — nothing to do. To let
the planner run the deepagents research subagent (multi-search cited reports), set
`DEEPAGENTS_ENABLED=1` in `.env` (or ask for it to be added to the Settings page). The Doctor page
reports both as live/declined with their fixes.

---

## 5. Keep the backend running (source runs and custom setups)

These matter when you run from source (way 1), keep a backend on your own machine, or customise
ports. An install pointing at the Hugging Face Space (§5.6) needs none of this.

Pick whichever suits you. All four work; they differ in how much you have to think about them.

### 5.1 A launcher script (simplest)

Create `scripts/start-dobot.bat` in the repo (the repo ships one you can copy):

```bat
@echo off
REM Start Dobot: backend in the background, then the desktop app.
setlocal
set REPO=%~dp0..
start "Dobot backend" /min cmd /c "cd /d %REPO%\backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8756"
timeout /t 4 /nobreak >nul
start "" "%LOCALAPPDATA%\Dobot\Dobot.exe"
endlocal
```

Adjust the second `start` line if you installed the app somewhere else, or use `"Dobot"` to launch it
from the Start menu. Double-click the `.bat` and you are running.

### 5.2 Task Scheduler at logon (recommended for daily use)

1. **Task Scheduler → Create Task**.
2. **General**: name it `Dobot backend`; tick *Run whether user is logged on or not*; tick *Run with
   highest privileges* only if you need it.
3. **Triggers**: *At log on*.
4. **Actions**: *Start a program* → `cmd.exe` with arguments
   `/c cd /d C:\path\to\Dobot\backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8756`
5. **Settings**: tick *If the task fails, restart every 1 minute*.

Then tick **Start Dobot with Windows** in Dobot's **Security** page so the UI comes up too. The two
together give you "Dobot is just always there".

### 5.3 A Windows service (runs before you log in)

Use [NSSM](https://nssm.cc/) to wrap the backend:

```bat
nssm install DobotBackend "C:\path\to\Dobot\backend\.venv\Scripts\python.exe" "-m uvicorn app.main:app --host 127.0.0.1 --port 8756"
nssm set DobotBackend AppDirectory "C:\path\to\Dobot\backend"
nssm start DobotBackend
```

Run those from an elevated prompt. This is the option to pick if you want automations to fire while
you are logged out.

### 5.4 Start it by hand

```bash
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8756
```

Perfect for occasional use, and for the first run while you are still deciding.

### 5.5 Deploy one backend, point every device at it

The alternative to bundling: run the backend **once** on a machine that is always on — a home
server, a small cloud VM, even a second PC on your LAN — and have every install (bundled or not)
connect to it.

```bash
# on the server (Python 3.11+ and uv installed)
git clone <your-repo> Dobot && cd Dobot
cp .env.example .env        # put the real API keys here — the server holds the secrets
uv sync --project backend
uv run --project backend uvicorn app.main:app --host 0.0.0.0 --port 8756
```

Then, on the server, set `DOBOT_API_TOKEN=<long random string>` in `.env` and generate clients
correspondingly — with a public host the open loopback default would invite strangers. Keep the
port firewalled to your devices or tunnel it (Tailscale/WireGuard are the lazy-correct options);
do not expose an unauthenticated Dobot to the internet.

On each device's app: **Settings → Backend**, set the URL to `http://<server>:8756` (or your tunnel
address), and paste the same token as the `dobot_api_token` key in **Settings → API keys**.

**What this trades away — read before choosing it.** Dobot is a *personal* operating layer: tools
such as `fs_*` and `computer_*` act on the machine the backend runs on, so with a deployed backend
they operate the **server**, not the device you are typing on. Screen-region questions and mic audio
also travel to the server. For shared reasoning, research and memory across devices this is a clean
setup; for an assistant that operates each device locally, run the backend on that device (§5.1–5.4)
or opt into the bundled installer (§4).

### 5.6 Deploy the backend to a Hugging Face Space

A hosted backend with no server of your own: the backend runs as a **private Docker Space**,
secrets live in the Space's environment, and the laptop holds nothing but a token. One command from
the repo root pushes it — the Space's name comes from `HF_SPACE` (or the git-ignored `.hf-space`
file at the repo root, so the command stays short on repeat runs):

```bash
HF_TOKEN=hf_xxx HF_SPACE=YOUR_SPACE bash scripts/deploy-hf-space.sh
```

The script creates the Space if it does not exist yet (private, Docker SDK), assembles the Space
repo in a temp directory — `Dockerfile`, the Space README card, `backend/`, `skills/` — and pushes
it; the push is what triggers the image build. `deploy/huggingface/` holds exactly what lands there.

Once the build is green:

1. **Space → Settings → Variables and secrets** — the operator side. As the person who runs the
   backend you supply the infrastructure (full table in the Space's README):

   | Secret | Why |
   | --- | --- |
   | `DOBOT_API_TOKEN` | **Recommended**: the same value as your HF read token (step 3), so both gates share the app's single token |
   | `MONGODB_URI` | durable memory that survives a Space rebuild |
   | `LANGSMITH_API_KEY` (+ `LANGSMITH_API_URL`, `LANGSMITH_PROJECT`) | usage tracing |
   | `DEEPAGENTS_ENABLED` | multi-search cited research reports |
   | `TRANSCRIBER_ENABLED=false` | when nobody can plug a microphone into the Space |

   `NEBIUS_API_KEY` and `TAVILY_API_KEY` are *not* required here: each person brings their own in
   the app (step 5). Setting them in the Space is a fallback for when the Space is a single person's
   backend — values saved in the Space's `.env` are exactly that fallback, and are also what the
   container loses on rebuild, which is why `MONGODB_URI` is the one to set as a secret.
2. Nothing secret is pushed to the repo or baked into the image — the backend reads every one of
   these as a plain environment variable.
3. Create a **read** token at <https://huggingface.co/settings/tokens>.
4. In the app: **Settings → Backend** → the Space URL the deploy script prints at the end → paste
   that token in the API-token field → **Save and reconnect**.
5. In the app: **Settings → Your API keys** → paste your own `nebius` (required) and `tavily`,
   `zilliz_token`, `zilliz_uri`. They land in the Space's `.env` and take effect immediately.
   `mongodb_uri` and the LangSmith values appear read-only under **Managed for you** — they are the
   operator's, and the API refuses to overwrite them.

Why this is safe: the Space is private, so Hugging Face's proxy rejects every request that arrives
without your token, and the backend's bearer gate (step 1) is a second, independent check behind it.

> **Paying for a shared Space.** Every user of one Space writes their keys into that one
> container's `.env` — the last save wins, and a rebuild clears them. For genuinely separate
> credentials each person should run their own backend: a local one (§5.1–5.4) or their own copy of
> the Space (duplicate this one). The Space URL is a Settings field precisely so this is a copy, not
> a fork.

Honest trade-offs — the same ones as §5.5: `fs_*` and `computer_*` act on the Space's container, not
your laptop; the container's filesystem is wiped when the Space rebuilds (set `MONGODB_URI` if
memories must survive); a sleeping free Space takes ~30 s to wake; and the mic/voice extras that
need a local model are not in the image — the Doctor page says so instead of pretending. Prefer
your own server: §5.5. Prefer everything on this machine: §5.1–5.4.

---

## 6. Autostart for the app itself

Dobot has this built in: **Security → Human control → Start Dobot with Windows**, or the same switch
in **Settings → Preferences**. It registers the app in the Windows startup entries for your user
account, and it unregisters when you turn it off.

When the dot is **off** and you close the chat window, Dobot quits — normal app behaviour. When the
dot is **on**, closing the chat window just hides it, and Dobot keeps running in the tray. To stop it
completely, use **Tray → Quit Dobot**, or **Ctrl+Shift+Esc** to stop the current task first.

---

## 7. Where your data lives

| What | Where | Notes |
| --- | --- | --- |
| API keys and settings | `<repo>/.env` | Gitignored. Backend-only; the UI never receives a key. On the Hugging Face Space (§5.6) they are Space **secrets** — environment variables, never a file. |
| Backend state (local record store, vectors, logs) | `<repo>/.dobot/` | `DOBOT_STATE_DIR` in `.env`. Space deploys: `/app/.dobot` inside the container, wiped when the Space rebuilds — set `MONGODB_URI` if memories must survive. |
| Skills | `<repo>/skills/` | `DOBOT_SKILLS_DIR`. One folder per skill. Space deploys: baked into the image (edits survive until the next rebuild). Bundled installers (`DOBOT_BUNDLE_SIDECAR=1`): a writable `skills\` next to the exe. |
| Floating-dot position and the Always-on choice | `%APPDATA%\com.dobot.desktop\dobot-state.json` | Written by the desktop shell. |
| Backend address override | browser `localStorage` key `dobot.baseUrl` | Change it in **Settings → Backend**. |
| Installed app | `%LOCALAPPDATA%\Dobot\` (NSIS) | Or wherever you chose during install. |

To reset Dobot completely: quit it, delete `.dobot/` and `%APPDATA%\com.dobot.desktop\`, and start it
again. Memory and task history reset; your `.env` and skills are untouched.

---

## 8. Updating

From source:

```bash
git pull
cd backend && uv pip install -e ".[dev]"
cd ../desktop && npm install && npm run tauri build
```

Then re-run the new installer. It upgrades in place; your `.env`, `.dobot/` and the app's state file
are left alone.

---

## 9. Uninstalling

- **Installed app**: *Settings → Apps → Installed apps → Dobot → Uninstall*.
- **Autostart**: if you ever enabled it, turn it off in **Security** *before* uninstalling so the
  startup entry is removed cleanly.
- **Leftovers you may want to delete by hand**: `%APPDATA%\com.dobot.desktop\`, `<repo>/.dobot/`, and
  the backend virtualenv `backend/.venv/`.
- **Stop the backend**: if you set up Task Scheduler or NSSM, delete the task / service too.

---

## 10. Optional upgrades

These are all genuinely optional — Dobot runs without each of them, with the trade-offs listed.

| Upgrade | How | What you gain | If you skip it |
| --- | --- | --- | --- |
| MongoDB | set `MONGODB_URI`, install the `mongo` extra | durable structured memory across reinstalls | local file store in `.dobot/` |
| Zilliz (Milvus) | set `ZILLIZ_URI` + `ZILLIZ_TOKEN`, install the `vectors` extra | scalable semantic recall | local cosine index over the file store |
| OCR | install the `ocr` extra and Tesseract | reading text out of screenshots | vision model path only |
| Hermes runtime | set `AGENT_RUNTIME=cli` or `remote`, `HERMES_ENDPOINT` | full browser automation and desktop (GUI) control | Dobot's own guarded local tools; `computer_*` refuses |
| NemoClaw / OpenShell | run the stack, then set `SANDBOX_PROVIDER=nemoclaw` and `OPENSHIELD_GATEWAY_URL` | kernel-level isolation for executed actions | in-process action firewall only (`isolation: none`) |

The **Security** page shows which of these are actually active, and explicitly flags the ones that are
not — so you never have to guess whether isolation is real.

---

## 11. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Sidebar says *backend offline* | The backend is not running, or it is on another port. Start it, or change the address in **Settings → Backend**. |
| The app cannot reach `…hf.space` | The Space is asleep or still building (first wake takes ~30 s) — check its logs on huggingface.co. Then confirm your HF read token is pasted in **Settings → Backend**; a private Space rejects every call without it. |
| Live status stays "reconnecting" | A private Space always refuses the WebSocket upgrade, and the app knows that: it polls `/events` over HTTP instead and shows *live (HTTP polling)* within ~3 s. If it never recovers, the token is missing or stale — re-paste your HF read token in **Settings → Backend**. |
| `dobot-backend.exe` is running but Dobot is closed | Only affects installers built with `DOBOT_BUNDLE_SIDECAR=1`: the app was ended forcefully (Task Manager) rather than quit, so it could not stop its bundled backend. It is harmless — quit Dobot normally next time, end that process, or just launch Dobot again (it adopts the running backend instead of starting a second one). |
| `[Errno 10048] address already in use` | Something already holds :8756. Find it with `netstat -ano \| findstr :8756`, or run the backend on another port and point Settings at it. |
| Answers arrive but research says offline | `TAVILY_API_KEY` is missing or rejected. Check `/health`. |
| `/chat/completions` returns 404 | `NEBIUS_BASE_URL` points at the legacy `api.studio.nebius.com` host. It must be `https://api.tokenfactory.nebius.com/v1`. |
| A hotkey does nothing | Another app owns the chord (often `Ctrl+Shift+S`). Dobot logs the failure and keeps running; change or free the shortcut. |
| The talk chord (`Ctrl+Shift+Space`) opens the panel but it never hears you | Microphone access was denied, or there is no input device — the panel says so beside the composer. Allow the microphone for Dobot in Windows settings (browser site settings in `npm run dev`) and press the chord again. |
| `Ctrl+Alt+L` reports it could not take a picture | A few windows refuse to be captured (DRM video, the secure desktop, or every window on screen belongs to Dobot). Fall back to `Ctrl+Shift+S` and select the region yourself. |
| Screen selection gives a blank image | On Windows, check the display is not in a protected/exclusive mode; on multi-GPU laptops, capture follows the monitor the region is on. |
| SmartScreen blocks the installer | Unsigned build — **More info → Run anyway**, or sign the bundle. |
| `cargo tauri build` fails on a missing linker | The MSVC C++ build tools are not installed. `npx tauri info` will confirm. |
| The dot is not on screen | It is off by default. Turn on **Always on** in the chat header, or **Tray → Always-on dot**. |
| Everything looks fine but nothing runs | You are probably in **Shadow mode**. It plans and previews without executing — that is the point. Turn it off in the composer or Security. |

---

## 12. Security notes for a laptop install

- The backend binds to `127.0.0.1` and has **no authentication**. Keep it that way on a laptop: do
  not change it to `0.0.0.0` and do not expose port 8756 to your network or the internet. A deployed
  backend is the exception and must not be: on your own server (§5.5) bind wider **and** set
  `DOBOT_API_TOKEN`, plus a firewall or tunnel; on the private Hugging Face Space (§5.6) HF's proxy
  and the backend's bearer gate (same token) are both in front of every request.
- Dobot asks before anything `HIGH` or `CRITICAL` risk. Do not get into the habit of clicking
  **Approve** without reading the preview — the preview is the whole safety mechanism.
- `SANDBOX_ALLOWED_PATHS` defaults to your home directory. Narrow it in `.env` if you want Dobot
  confined to specific folders.
- Try anything unfamiliar in **Shadow mode** first: you get the full plan and a preview of every action
  with nothing executed.
- Keep `.env` out of cloud-synced folders and out of git.
- If you enabled autostart or a service, remember the backend is then running from boot — including
  its scheduled automations. Review **Automations** occasionally.
