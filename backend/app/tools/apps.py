"""Application launching.

`app_open` is LOW risk because launching an application is visible and immediately reversible, but it
still goes through the sandbox for path checks and is verified by looking for the process afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shlex
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
    "telegram": {"win32": "telegram", "darwin": "Telegram", "linux": "telegram-desktop"},
}

PROCESS_HINTS = {
    "code": "Code.exe",
    "msedge": "msedge.exe",
    "chrome": "chrome.exe",
    "explorer": "explorer.exe",
    "notepad": "notepad.exe",
    "calc": "CalculatorApp.exe",
    "wt": "WindowsTerminal.exe",
    "telegram": "Telegram.exe",
}

#: Human phrasings the model reaches for that are not alias keys — e.g. it asked for "File Explorer"
#: and got the literal string launched. Keyed by the same normalisation as the alias table.
APP_SYNONYMS: dict[str, str | None] = {
    "fileexplorer": "explorer",
    "windowsexplorer": "explorer",
    "filemanager": "explorer",
    "webbrowser": "browser",
    "internetbrowser": "browser",
    "visualstudiocode": "vscode",
    "telegramdesktop": "telegram",
    "texteditor": "notepad",
    "wordpad": "notepad",
    "taskmanager": None,  # not an alias we can launch reliably; rejected below
}

#: Name suffixes that mean "this is a path, not an application name".
_PATH_SUFFIXES = (".exe", ".bat", ".cmd", ".lnk", ".com")

#: A target that is a web address rather than a file — browsers take both.
_URL_SCHEME = re.compile(r"(?i)^https?://\S+$")

#: In `tasklist /V` output, the window title is the field after CPU time (e.g. ``0:00:32 Title``).
_TASKLIST_TITLE = re.compile(r"\s\d+:\d{2}:\d{2}\s+(.+?)\s*$")


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _resolve_alias(app: str) -> str | None:
    """Map whatever the caller said onto an APP_ALIASES key, or None if it isn't a known app."""
    normalised = _normalise(app)
    if normalised in APP_ALIASES:
        return normalised
    # Accept phrases with spaces too ("file explorer"), by normalising the synonyms the same way.
    synonym = APP_SYNONYMS.get(app.strip().lower()) or APP_SYNONYMS.get(normalised)
    if synonym:
        return synonym
    for key in APP_ALIASES:  # last resort: substring, e.g. "visual studio code (insiders)"
        if len(key) >= 4 and key in normalised:
            return key
    return None


def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _windows_installed_binary(binary: str) -> str:
    """Resolve apps that commonly install outside PATH on Windows."""
    if binary.lower() != "telegram":
        return binary
    found = shutil.which(binary)
    if found:
        return found
    roots = [os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")]
    candidates = [
        Path(root) / "Telegram Desktop" / "Telegram.exe"
        for root in roots
        if root
    ]
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Telegram Desktop" / "Telegram.exe")
    return next((str(path) for path in candidates if path.is_file()), binary)


async def _discover_windows_app(app: str, ctx: ToolContext) -> tuple[str, str, str] | None:
    """Find an arbitrary Start-menu application without maintaining an alias list.

    ``Get-StartApps`` is Windows' own application inventory and includes packaged/UWP apps as well
    as ordinary desktop shortcuts. We only read the inventory; the returned AppID is then opened by
    Explorer's ``shell:AppsFolder`` handler. Exact matches win, and fuzzy matches are accepted only
    when they identify one application.
    """
    command = (
        "powershell.exe -NoProfile -NonInteractive -Command "
        '"$apps = Get-StartApps | Select-Object Name,AppID; '
        '$apps | ConvertTo-Json -Compress"'
    )
    try:
        result = await ctx.sandbox.run_command(command, timeout=10, token=ctx.token)
        apps = json.loads(result.stdout)
    except Exception:  # noqa: BLE001 - discovery is an optional convenience
        return None
    if isinstance(apps, dict):
        apps = [apps]
    if not isinstance(apps, list):
        return None
    wanted = _normalise(app)
    records = [
        item
        for item in apps
        if isinstance(item, dict) and isinstance(item.get("Name"), str) and isinstance(item.get("AppID"), str)
    ]
    exact = [item for item in records if _normalise(item["Name"]) == wanted]
    matches = exact or [item for item in records if wanted in _normalise(item["Name"])]
    if len(matches) != 1:
        return None
    item = matches[0]
    app_id = item["AppID"].replace('"', "")
    if not app_id:
        return None
    return "explorer.exe", f"shell:AppsFolder\\{app_id}", str(item["Name"])


class AppOpenTool(Tool):
    name = "app_open"
    description = (
        "Open an installed application, executable path, browser, or folder. On Windows, "
        "application names are resolved through PATH and the Start-menu app inventory, so an "
        "alias is optional."
    )
    risk_floor = RiskLevel.LOW
    proactive_ok = True
    parameters = {
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "Application name, executable/path, or friendly alias, e.g. 'vscode' or 'Telegram'",
            },
            "target": {
                "type": "string",
                "description": (
                    "Optional file/folder to open in that app, or an http(s) URL to open in a browser"
                ),
            },
        },
        "required": ["app"],
    }

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ExecutionResult:
        started = time.perf_counter()
        ctx.check_cancelled()
        app = self.require(params, "app").strip()
        target = params.get("target")
        # Model-supplied text must never become shell syntax. A double quote is the one character
        # that breaks out of a quoted cmd.exe argument (POSIX launches lean on this guarantee for
        # their shlex quoting too), so a value carrying one is refused before any command exists.
        for label, value in (("app", app), ("target", target)):
            if value is not None and '"' in str(value):
                return self.fail(
                    self.name,
                    f"{label} contains a double quote, which cannot be launched safely",
                    started,
                )
        platform = _platform_key()
        resolved = _resolve_alias(app)
        looks_like_path = app.lower().endswith(_PATH_SUFFIXES) or "/" in app or "\\" in app
        discovered_target = ""
        discovered_name = ""
        if resolved:
            binary = APP_ALIASES[resolved].get(platform, app)
            if platform == "win32":
                binary = _windows_installed_binary(binary)
                # An alias is only a hint, not proof that the executable is on PATH. This
                # fallback is what lets Store/Start-menu apps such as Telegram launch without
                # adding every application's install location to Dobot.
                if not shutil.which(binary) and not Path(binary).is_file():
                    discovered = await _discover_windows_app(app, ctx)
                    if discovered:
                        binary, discovered_target, discovered_name = discovered
        elif looks_like_path:
            binary = app  # an explicit path to an executable: launch it as given
        else:
            # Let Windows resolve arbitrary installed applications through its own Start-menu
            # inventory, so every new app does not require an alias or a Dobot release.
            if platform == "win32":
                path_binary = shutil.which(app)
                discovered = (
                    (path_binary, "", Path(path_binary).stem)
                    if path_binary
                    else await _discover_windows_app(app, ctx)
                )
                if discovered:
                    binary, discovered_target, discovered_name = discovered
                else:
                    known = ", ".join(sorted(APP_ALIASES))
                    return self.fail(
                        self.name,
                        f"application {app!r} was not found in Windows apps, PATH, or known aliases "
                        f"(shortcuts: {known})",
                        started,
                    )
            else:
                return self.fail(
                    self.name,
                    f"application {app!r} was not found on {platform}; provide its executable path",
                    started,
                )
        resolved_target = discovered_target if "discovered_target" in locals() else ""
        target_kind = "application" if resolved_target else "path"

        if target:
            target = str(target).strip()
            if _URL_SCHEME.match(target):
                # A URL is not a filesystem path: routing it through check_path resolved it
                # relative to the working directory and then refused it as "outside the allowed
                # roots", so opening a browser at a page never worked.
                if _normalise(binary) in {"explorer", "finder"}:
                    return self.fail(
                        self.name,
                        f"{binary} cannot open a URL — use app='browser' for {target}",
                        started,
                    )
                resolved_target = target
                target_kind = "url"
            else:
                try:
                    assert ctx.sandbox is not None
                    resolved_target = str(await ctx.sandbox.check_path(target))
                except Exception as exc:  # noqa: BLE001
                    return self.fail(self.name, f"target not usable: {exc}", started)

        command = self._build_command(binary, platform, resolved_target)
        if command is None:
            return self.fail(self.name, f"cannot launch {app} on {platform}", started)

        assert ctx.sandbox is not None
        # For a URL launch, snapshot window titles *before* the browser opens. Without it, a
        # browser that was already running would make verify() pass on a process that had nothing
        # to do with this request.
        titles_before: set[str] = set()
        if target_kind == "url":
            titles_before = await self._window_titles(
                ctx, self._process_hint(binary) or ""
            )
        result = await ctx.sandbox.run_command(command, timeout=25, token=ctx.token)
        # Launchers commonly return non-zero on Windows even when the app starts, so success is
        # determined by the process probe below rather than by the exit code. A *timeout* is
        # different: `start` returns immediately for a healthy launch, so -9 means it hung.
        if result.exit_code == -9:
            return self.fail(
                self.name,
                f"launcher did not return: {result.stderr.strip()[:200] or command}",
                started,
            )
        return self.ok(
            self.name,
            {
                "app": app,
                "discovered_name": discovered_name if "discovered_name" in locals() else "",
                "binary": binary,
                "command": command,
                "target": resolved_target,
                "target_kind": target_kind,
                "exit_code": result.exit_code,
                "stderr": result.stderr[:400],
                "process_hint": self._process_hint(binary),
                "window_titles_before": sorted(titles_before),
            },
            started,
        )

    @staticmethod
    def _process_hint(binary: str) -> str:
        """Image name the verifier should look for — from the alias table, or the resolved exe."""
        hint = PROCESS_HINTS.get(Path(binary).stem.lower())
        if hint:
            return hint
        resolved = shutil.which(binary)
        if resolved:
            return Path(resolved).name
        # A bare name is a reasonable guess; a path-like or spaced string is not (tasklist would
        # never match it), so report nothing and let verify() say "no way to probe".
        base = Path(binary).name
        return base if " " not in base and "\\" not in base and "/" not in base else ""

    @staticmethod
    def _build_command(binary: str, platform: str, target: str) -> str | None:
        # Windows: inputs are quote-free (rejected in run()), so a double-quoted cmd.exe argument
        # cannot be broken out of — `&`, `|`, `<`, `>` stay literal inside the quotes.
        if platform == "win32":
            exe = shutil.which(binary) or binary
            if exe.lower() in {"explorer", "explorer.exe"}:
                return f'explorer "{target}"' if target else "explorer"
            quoted_target = f'"{target}"' if target else ""
            # Detach the child from the backend's stdio handles. GUI applications such as Notepad
            # otherwise inherit the pipe and make asyncio.communicate wait until the app exits.
            return f'start "" /B "{exe}" {quoted_target} >NUL 2>&1'.strip()
        # POSIX: shlex-quote every model-supplied piece — `$`, backticks and friends are inert
        # inside single quotes, so no character in a name or URL reaches the shell unparsed.
        if platform == "darwin":
            command = f"open -a {shlex.quote(binary)}"
            return f"{command} {shlex.quote(target)}".strip() if target else command
        exe = shutil.which(binary) or binary
        command = shlex.quote(exe)
        return f"{command} {shlex.quote(target)}".strip() if target else command

    @staticmethod
    async def _window_titles(ctx: ToolContext, hint: str) -> set[str]:
        """Window titles for a process image, empty when the platform can't report them.

        `tasklist /V` is the only built-in on Windows that exposes titles; elsewhere this is a
        no-op and verification degrades to the process probe alone.
        """
        if not hint or _platform_key() != "win32":
            return set()
        assert ctx.sandbox is not None
        try:
            probe = await ctx.sandbox.run_command(
                f'tasklist /V /FI "IMAGENAME eq {hint}"', timeout=15
            )
        except Exception:  # noqa: BLE001
            return set()
        titles: set[str] = set()
        for line in probe.stdout.splitlines():
            # tasklist /V columns: Image Name, PID, Session, Mem, Status, User, CPU Time, Title.
            # The window title is whatever follows the CPU-time field, and may contain spaces.
            match = _TASKLIST_TITLE.search(line)
            if not match:
                continue
            title = match.group(1).strip()
            if title and title.upper() != "N/A":
                titles.add(title)
        return titles

    async def verify(
        self, params: dict[str, Any], result: ExecutionResult, ctx: ToolContext
    ) -> VerificationResult:
        output = result.output or {}
        hint = str(output.get("process_hint", ""))
        checks: list[CheckResult] = []
        if not hint:
            # Nothing to probe against. This is *unverifiable*, not a failure: the launch command
            # itself completed, and the orchestrator records skipped verification separately.
            return VerificationResult(
                verified=False,
                checks=[],
                skipped_reason="no way to probe this application",
            )
        assert ctx.sandbox is not None
        probe = (
            f'tasklist /FI "IMAGENAME eq {hint}"'
            if _platform_key() == "win32"
            else f"pgrep -fl {shlex.quote(Path(hint).stem)}"
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
        notes = "" if running else "application did not appear in the process list yet"

        # A browser that was already open would satisfy process_running regardless of this
        # request, so for a URL launch compare window titles against the pre-launch snapshot.
        # Nothing new appearing is *inconclusive* — the browser may have reused an open window —
        # so it is reported as unverifiable rather than as a failure.
        if str(output.get("target_kind", "")) == "url" and running:
            before = set(output.get("window_titles_before") or [])
            after = await self._window_titles(ctx, hint)
            if not after:
                return VerificationResult(
                    verified=False,
                    checks=checks,
                    skipped_reason="window titles are not readable on this platform",
                    notes="browser process is running; the loaded page was not independently confirmed",
                )
            appeared = sorted(after - before)
            if appeared:
                title = appeared[0][:120]
                checks.append(
                    CheckResult(
                        name="window_opened_for_target",
                        passed=True,
                        detail=title,
                    )
                )
                # The title proves a window appeared — not that this URL finished loading.
                # Say exactly that, so the composed answer has no room to overclaim.
                notes = (
                    f"a new window appeared with title {title!r}; the browser process is running. "
                    "The URL itself was not independently confirmed"
                )
            else:
                return VerificationResult(
                    verified=False,
                    checks=checks,
                    skipped_reason=(
                        "browser was already open and no new window title appeared; the loaded "
                        "page was not independently confirmed"
                    ),
                    notes="browser process is running",
                )

        return VerificationResult(verified=all(c.passed for c in checks), checks=checks, notes=notes)

    async def preview(self, params: dict[str, Any], ctx: ToolContext) -> list[str]:
        return [f"Open {params.get('app')}" + (f" with {params.get('target')}" if params.get("target") else "")]
