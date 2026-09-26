"""Application launching.

`app_open` is LOW risk because launching an application is visible and immediately reversible, but it
still goes through the sandbox for path checks and is verified by looking for the process afterwards.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Any

from app.schemas import CheckResult, ExecutionResult, RiskLevel, VerificationResult
from app.tools.base import Tool, ToolContext

#: Friendly names → what to actually launch. Extend as needed; unmatched names are treated as paths.
APP_ALIASES: dict[str, dict[str, str]] = {
    "vscode": {"win32": "code", "darwin": "Visual Studio Code", "linux": "code"},
    "code": {"win32": "code", "darwin": "Visual Studio Code", "linux": "code"},
    "browser": {"win32": "msedge", "darwin": "Safari", "linux": "xdg-open"},
    "chrome": {"win32": "chrome", "darwin": "Google Chrome", "linux": "google-chrome"},
    "edge": {"win32": "msedge", "darwin": "Microsoft Edge", "linux": "microsoft-edge"},
    "terminal": {"win32": "wt", "darwin": "Terminal", "linux": "x-terminal-emulator"},
    "explorer": {"win32": "explorer", "darwin": "Finder", "linux": "xdg-open"},
    "files": {"win32": "explorer", "darwin": "Finder", "linux": "xdg-open"},
    "notepad": {"win32": "notepad", "darwin": "TextEdit", "linux": "gedit"},
    "calculator": {"win32": "calc", "darwin": "Calculator", "linux": "gnome-calculator"},
}

PROCESS_HINTS = {
    "code": "Code.exe",
    "msedge": "msedge.exe",
    "chrome": "chrome.exe",
    "explorer": "explorer.exe",
    "notepad": "notepad.exe",
    "calc": "CalculatorApp.exe",
    "wt": "WindowsTerminal.exe",
}


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


class AppOpenTool(Tool):
    name = "app_open"
    description = "Open an application (e.g. VS Code, a browser, the file explorer) or a folder in it."
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "app": {"type": "string", "description": "Application name or alias, e.g. 'vscode'"},
            "target": {"type": "string", "description": "Optional file or folder to open in that app"},
        },
        "required": ["app"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        app = self.require(params, "app").strip()
        target = params.get("target")
        platform = _platform_key()
        binary = APP_ALIASES.get(app.lower(), {}).get(platform, app)
        resolved_target = ""

        if target:
            try:
                assert ctx.sandbox is not None
                resolved_target = str(await ctx.sandbox.check_path(str(target)))
            except Exception as exc:  # noqa: BLE001
                return self.fail(self.name, f"target not usable: {exc}", started)

        command = self._build_command(binary, platform, resolved_target)
        if command is None:
            return self.fail(self.name, f"cannot launch {app} on {platform}", started)

        assert ctx.sandbox is not None
        result = await ctx.sandbox.run_command(command, timeout=25, token=ctx.token)
        # Launchers commonly return non-zero on Windows even when the app starts, so success is
        # determined by the process probe below rather than by the exit code.
        return self.ok(
            self.name,
            {
                "app": app,
                "binary": binary,
                "command": command,
                "target": resolved_target,
                "exit_code": result.exit_code,
                "stderr": result.stderr[:400],
                "process_hint": PROCESS_HINTS.get(binary, binary),
            },
            started,
        )

    @staticmethod
    def _build_command(binary: str, platform: str, target: str) -> str | None:
        quoted_target = f'"{target}"' if target else ""
        if platform == "win32":
            exe = shutil.which(binary) or binary
            if exe.lower() in {"explorer", "explorer.exe"}:
                return f'explorer "{target}"' if target else "explorer"
            return f'start "" "{exe}" {quoted_target}'.strip()
        if platform == "darwin":
            base = f'open -a "{binary}"'
            return f"{base} {quoted_target}".strip()
        exe = shutil.which(binary) or binary
        return f"{exe} {quoted_target}".strip()

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        hint = str(output.get("process_hint", ""))
        checks: list[CheckResult] = []
        if not hint:
            return VerificationResult(
                verified=False,
                checks=[CheckResult(name="process_probe", passed=False, detail="no process hint")],
                skipped_reason="no way to probe this application",
            )
        assert ctx.sandbox is not None
        probe = (
            f'tasklist /FI "IMAGENAME eq {hint}"'
            if _platform_key() == "win32"
            else f"pgrep -fl {Path(hint).stem}"
        )
        try:
            probe_result = await ctx.sandbox.run_command(probe, timeout=15)
            running = hint.lower() in probe_result.stdout.lower()
        except Exception:  # noqa: BLE001
            running = False
        checks.append(
            CheckResult(
                name="process_running",
                passed=running,
                detail=f"probe: {hint}",
            )
        )
        return VerificationResult(
            verified=running,
            checks=checks,
            notes="" if running else "application did not appear in the process list yet",
        )

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        return [f"Open {params.get('app')}" + (f" with {params.get('target')}" if params.get("target") else "")]
