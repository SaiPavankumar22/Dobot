# Installing Dobot on your laptop

This is the practical guide: how to go from the source checkout to something you double-click.

Dobot is **two processes**:

| Process | What it is | What it does |
| --- | --- | --- |
| **Backend** | FastAPI app (Python) on `127.0.0.1:8756` | All reasoning, memory, tools and safety decisions |
| **Desktop app** | Tauri shell + web UI (`Dobot.exe`) | The chat window, the floating dot, hotkeys, screen capture |

The desktop app is the part you install. The backend has to be running for Dobot to think — and today
Dobot does **not** start it for you, so pick a way to keep it running from
[Keep the backend running](#keep-the-backend-running). Wiring the backend in as a Tauri *sidecar* so a
single installer does both is the natural next step and is not done yet.

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

## 3. Option A — run it from source (fastest, no installer)

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

## 4. Option B — build a real installer

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
desktop/src-tauri/target/release/bundle/msi/Dobot_0.1.0_x64_en-US.msi
desktop/src-tauri/target/release/bundle/nsis/Dobot_0.1.0_x64-setup.exe
desktop/src-tauri/target/release/dobot-desktop.exe        # the raw binary
```

Pick either: the **NSIS `.exe`** is the friendlier one (per-user install, no admin prompt), the
**MSI** is better if you manage machines with policy.

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

### First run

1. Make sure the backend is running (§5).
2. Launch **Dobot**. The **chat window** opens — sidebar, welcome screen, composer. No dot.
3. Turn on **Always on** in the header if you want the floating dot.
4. Dobot's status chip in the sidebar footer reads *backend connected* or *backend offline*. If it is
   offline, the composer shows the exact command to start it.

### Adding API keys after packaging (no terminal needed)

A packaged install does not assume you will ever open a terminal or edit a file by hand. In the app:

**Settings → API keys** — paste a key, press **Save**. It is persisted to the backend's `.env` and
applied to the running process immediately (no restart). The field shows only ever-shows a masked
hint like `sk-1…ab12 (48 chars)`, never the key itself; **Clear** blanks it.

What you typically set there:

| Key | Why |
| --- | --- |
| `nebius` | **Required.** Reasoning. Get one at studio.nebius.com |
| `tavily` | Web research |
| `langsmith` | Usage monitoring in LangSmith (optional) |
| `zilliz_token`, `mongodb_uri`, `zilliz_uri` | Durable/semantic memory (optional) |
| `laya_server_url` | The laya-serve sidecar, e.g. `http://127.0.0.1:8000` (optional) |

If you prefer the terminal anyway, editing `.env` in the repo root and restarting the backend does
exactly the same thing — see [`prerequisites.md`](prerequisites.md) for every variable.

### Optional: voice input (mic button in chat and on the widget)

The mic button transcribes speech **locally** with CrisperWhisper 2.0 small (~500 MB, downloaded on
first use; audio never leaves the machine). To enable it:

```bash
cd backend && uv pip install faster-whisper
```

Also install [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) and put it on PATH — browser and WebView
recordings arrive as webm/ogg, which the model needs ffmpeg to decode. Check the **Doctor** page:
it reports exactly what is missing. Without it, the mic button explains why instead of silently
failing.

### Optional: deep research and the LangGraph harness

Both ride on `langgraph`, which is already in the backend's dependencies — nothing to do. To let
the planner run the deepagents research subagent (multi-search cited reports), set
`DEEPAGENTS_ENABLED=1` in `.env` (or ask for it to be added to the Settings page). The Doctor page
reports both as live/declined with their fixes.

---

## 5. Keep the backend running

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
| API keys and settings | `<repo>/.env` | Gitignored. Backend-only; the UI never receives a key. |
| Backend state (local record store, vectors, logs) | `<repo>/.dobot/` | `DOBOT_STATE_DIR` in `.env`. |
| Skills | `<repo>/skills/` | `DOBOT_SKILLS_DIR`. One folder per skill. |
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
| `[Errno 10048] address already in use` | Something already holds :8756. Find it with `netstat -ano \| findstr :8756`, or run the backend on another port and point Settings at it. |
| Answers arrive but research says offline | `TAVILY_API_KEY` is missing or rejected. Check `/health`. |
| `/chat/completions` returns 404 | `NEBIUS_BASE_URL` points at the legacy `api.studio.nebius.com` host. It must be `https://api.tokenfactory.nebius.com/v1`. |
| A hotkey does nothing | Another app owns the chord (often `Ctrl+Shift+S`). Dobot logs the failure and keeps running; change or free the shortcut. |
| Screen selection gives a blank image | On Windows, check the display is not in a protected/exclusive mode; on multi-GPU laptops, capture follows the monitor the region is on. |
| SmartScreen blocks the installer | Unsigned build — **More info → Run anyway**, or sign the bundle. |
| `cargo tauri build` fails on a missing linker | The MSVC C++ build tools are not installed. `npx tauri info` will confirm. |
| The dot is not on screen | It is off by default. Turn on **Always on** in the chat header, or **Tray → Always-on dot**. |
| Everything looks fine but nothing runs | You are probably in **Shadow mode**. It plans and previews without executing — that is the point. Turn it off in the composer or Security. |

---

## 12. Security notes for a laptop install

- The backend binds to `127.0.0.1` and has **no authentication**. Keep it that way: do not change it to
  `0.0.0.0` and do not expose port 8756 to your network or the internet.
- Dobot asks before anything `HIGH` or `CRITICAL` risk. Do not get into the habit of clicking
  **Approve** without reading the preview — the preview is the whole safety mechanism.
- `SANDBOX_ALLOWED_PATHS` defaults to your home directory. Narrow it in `.env` if you want Dobot
  confined to specific folders.
- Try anything unfamiliar in **Shadow mode** first: you get the full plan and a preview of every action
  with nothing executed.
- Keep `.env` out of cloud-synced folders and out of git.
- If you enabled autostart or a service, remember the backend is then running from boot — including
  its scheduled automations. Review **Automations** occasionally.
