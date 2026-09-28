# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Dobot backend sidecar.
#
# Produces backend/dist/dobot-backend.exe — a single self-contained FastAPI server that the desktop
# installer places next to Dobot.exe (Tauri externalBin). Build it with
# scripts/build-backend-exe.bat, or directly:  cd backend && uv pip install pyinstaller &&
# uv run pyinstaller --noconfirm --clean dobot-backend.spec
#
# Known trade-off (measured): onefile unpacks to a temp dir on every launch, which adds a few
# seconds to first paint. The app connects lazily and shows "backend starting…" meanwhile, so the
# wait is cosmetic rather than blocking.

import os

block_cipher = None

# Ship the built-in skills inside the exe. At runtime app.config prefers a writable skills/ folder
# next to the executable (the desktop bundler provides one) and falls back to this embedded copy.
skills_src = os.path.abspath(os.path.join(SPECPATH, "..", "skills"))

a = Analysis(
    ["runner.py"],
    pathex=[os.path.join(os.getcwd(), "app")],
    binaries=[],
    datas=[(skills_src, "skills")],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "app.api.chat",
        "app.api.screen",
        "app.api.research",
        "app.api.tasks",
        "app.api.automations",
        "app.api.approvals",
        "app.api.memory",
        "app.api.skills",
        "app.api.settings",
        "app.api.security",
        "app.api.activity",
        "app.api.system",
        "app.api.ws",
        "langgraph.graph.state",
        "langgraph.checkpoint.memory",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="dobot-backend",
    console=True,  # the shell captures stdout/stderr for diagnostics; a window would hide them
    disable_windowed_traceback=False,
    upx=False,
    icon=None,
)
