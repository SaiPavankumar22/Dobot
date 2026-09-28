"""PyInstaller entry point for the bundled backend sidecar.

The desktop installer ships this as ``dobot-backend.exe`` next to ``Dobot.exe`` (Tauri
``externalBin``). The shell starts it on launch, waits for the API port to accept connections and
stops it on exit — see ``desktop/src-tauri/src/backend.rs``.

One small module on purpose: everything real lives in ``app.main``. This only adapts argv
(``--host`` / ``--port``) and keeps the process alive the way ``uvicorn`` would. Where ``.env``,
``skills/`` and ``.dobot/`` are resolved from is decided by :mod:`app.config` (``DOBOT_HOME``).

Build it with ``scripts/build-backend-exe.bat`` (from the repo root) or directly:

    cd backend
    uv pip install pyinstaller
    uv run pyinstaller --noconfirm --clean dobot-backend.spec
"""

from __future__ import annotations

import sys


def _arg_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def main() -> int:
    import uvicorn

    from app.config import get_settings
    from app.logging_setup import configure_logging
    from app.main import app

    settings = get_settings()
    configure_logging()
    args = sys.argv[1:]
    host = _arg_value(args, "--host") or settings.dobot_host
    port = int(_arg_value(args, "--port") or settings.dobot_port)
    print(f"dobot-backend: serving on http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level=settings.log_level.lower())
    return 0


if __name__ == "__main__":  # pragma: no cover - packaged entrypoint
    raise SystemExit(main())
