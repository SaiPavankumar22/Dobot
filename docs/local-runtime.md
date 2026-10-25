# Dobot local runtime

Dobot is one desktop experience with a small internal process graph:

```text
Dobot.exe (Tauri shell)
  └─ supervises dobot-backend.exe (FastAPI)
       ├─ local guarded tools and memory
       ├─ Playwright browser profile
       ├─ optional Hermes runtime
       └─ optional local model runtime
```

The shell adopts a healthy backend already listening on `127.0.0.1:8756`, otherwise it starts the
bundled sidecar. It waits for `/health`, not only for a listening TCP port, so the UI does not
mistake a partially initialized backend for a ready one. It stops only the process it started.

## Build modes

The normal local installer contains the backend:

```powershell
cd desktop
npm install
npm run sidecar
npm run tauri build
```

The build fails if `backend/dist/dobot-backend.exe` is missing. For an intentionally thin installer
that connects to a hosted backend, use:

```powershell
$env:DOBOT_CLOUD_ONLY = "1"
npm run tauri build
```

## Runtime inspection

The backend exposes:

- `GET /health` — liveness and provider posture.
- `GET /capabilities` — browser, Hermes, screen, files, voice, sandbox and backend location.
- `GET /runtime` — lifecycle state, process ID, version and data paths.
- `GET /runtime/profile` — conservative hardware-aware local/cloud/hybrid guidance.

The Settings page displays capabilities and compute guidance. A recommendation is not a model
installation: model manifests must still declare their RAM, VRAM, license and disk requirements.

## User data

The source checkout keeps its existing `.dobot` layout. Frozen installs use `%LOCALAPPDATA%\Dobot`
for mutable state:

```text
%LOCALAPPDATA%\Dobot\
  data\       memory and task state
  logs\       rotating backend.log and diagnostics
  models\     downloaded local model cache
  .dobot\     compatibility state for existing installations
```

Set `DOBOT_HOME` for a portable or test installation. API keys remain backend-owned and are never
returned by the settings API; diagnostic logs apply the configured secret redactor.

## Hardware expectations

The local backend, memory, browser and basic voice features can run on an 8 GB laptop. A large
Nemotron-class model should not be downloaded to that machine by default. Use cloud or hybrid
reasoning, or choose a small quantized CPU model after the setup wizard has checked available
resources. Hosted mode cannot access the laptop's files, screen, browser profile or Windows apps.
