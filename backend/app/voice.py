"""Spoken answers, using the speech engine already on the machine.

No new dependency, no network, no API key: Windows has SAPI, macOS has ``say``, and Linux almost always
has ``espeak-ng`` or ``spd-say``. If none of them is present, :func:`probe_voice` says so plainly and
:class:`Voice` refuses to pretend — the Doctor page reports it as broken rather than silently doing
nothing, which is the difference between a feature being absent and a feature appearing broken.

Two safety properties are deliberate:

* **Off by default.** An assistant that starts talking unprompted is startling at best. Speech is
  opt-in (``VOICE_ENABLED``) and can be triggered per-request from the UI.
* **Nothing is ever interpolated into a shell command.** Untrusted text - a model answer, a page title,
  a filename - goes into a temp file that the engine is told to read, and the engine is launched with
  an argument vector, never a shell string. That removes the injection class entirely rather than
  escaping against it.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)

SPEAK_TIMEOUT_SECONDS = 90
#: Markdown that would be read aloud literally ("asterisk asterisk") rather than spoken naturally.
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_MARKS = re.compile(r"[*_#>~|]+")
_URL = re.compile(r"https?://\S+")
_WS = re.compile(r"\s+")


def prepare_for_speech(text: str, *, max_chars: int = 1200) -> str:
    """Turn a markdown answer into something a person would actually say out loud."""
    cleaned = _FENCE.sub(" (code block omitted) ", text or "")
    cleaned = _LINK.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE.sub(r"\1", cleaned)
    cleaned = _URL.sub(" a link ", cleaned)
    cleaned = _MD_MARKS.sub(" ", cleaned)
    cleaned = _WS.sub(" ", cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0] + " …"
    return cleaned


@dataclass
class VoiceResult:
    ok: bool
    engine: str = ""
    spoken_chars: int = 0
    detail: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "spoken_chars": self.spoken_chars,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass
class VoicePlan:
    """The concrete command for this machine, or the reason there isn't one."""

    engine: str = ""
    argv: list[str] = field(default_factory=list)
    needs_file: bool = True
    fix: str = ""
    detail: str = ""


def _powershell() -> str:
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    return ""


def plan_voice(settings: Any = None) -> VoicePlan:
    """Pick the engine for this machine. Pure inspection - runs nothing."""
    system = platform.system().lower()
    name = (getattr(settings, "voice_name", "") or "").strip()
    rate = int(getattr(settings, "voice_rate", 0) or 0)

    if system == "windows":
        shell = _powershell()
        if not shell:
            return VoicePlan(
                engine="",
                fix="PowerShell is required for Windows speech. It ships with Windows; check it is on PATH.",
                detail="powershell.exe / pwsh not found on PATH.",
            )
        select = f"$s.SelectVoice('{name}'); " if name and name.isprintable() else ""
        # The text is read from the file we create; only our own generated path is interpolated.
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {max(-10, min(10, rate))}; "
            f"{select}"
            "try { $s.Speak([System.IO.File]::ReadAllText($args[0])) } finally { $s.Dispose() }"
        )
        return VoicePlan(
            engine="windows-sapi",
            argv=[shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            detail="System.Speech synthesizer, offline.",
        )

    if system == "darwin":
        say = shutil.which("say")
        if not say:
            return VoicePlan(engine="", fix="The `say` binary was not found.", detail="say missing")
        argv = [say, "-r", str(180 + rate * 10)]
        if name:
            argv += ["-v", name]
        return VoicePlan(engine="macos-say", argv=argv, detail="macOS `say`, offline.")

    for candidate, args in (("espeak-ng", ["-s", str(175 + rate * 10)]), ("espeak", ["-s", str(175 + rate * 10)]), ("spd-say", ["-r", str(rate)])):
        found = shutil.which(candidate)
        if found:
            return VoicePlan(engine=f"linux-{candidate}", argv=[found, *args], detail=f"{candidate}, offline.")
    return VoicePlan(
        engine="",
        fix="Install espeak-ng (Debian/Ubuntu: sudo apt install espeak-ng) or spd-say.",
        detail="No speech engine found on PATH.",
    )


def probe_voice(settings: Any = None) -> dict[str, Any]:
    """What the Doctor page needs to report, without speaking anything."""
    plan = plan_voice(settings)
    return {
        "engine": plan.engine,
        "detail": plan.detail,
        "fix": plan.fix,
        "available": bool(plan.engine),
        "enabled": bool(getattr(settings, "voice_enabled", False)),
        "platform": platform.system(),
    }


class Voice:
    """Speaks text, one utterance at a time. Never raises."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._plan: VoicePlan | None = None
        self._lock = asyncio.Lock()
        self._spoken = 0
        self._failures = 0
        self._last: VoiceResult | None = None

    @property
    def plan(self) -> VoicePlan:
        if self._plan is None:
            self._plan = plan_voice(self.settings)
        return self._plan

    @property
    def available(self) -> bool:
        return bool(self.plan.engine)

    async def speak(self, text: str, *, force: bool = False) -> VoiceResult:
        """Speak ``text``. Refuses politely when voice is off, unless ``force`` is set (an explicit test)."""
        started = time.perf_counter()
        if not force and not getattr(self.settings, "voice_enabled", False):
            return VoiceResult(
                ok=False,
                detail="Voice is disabled. Enable it in Settings (VOICE_ENABLED=true) or pass force.",
            )
        plan = self.plan
        if not plan.engine:
            self._failures += 1
            return VoiceResult(ok=False, detail=plan.fix or plan.detail or "no speech engine available")

        limit = int(getattr(self.settings, "voice_max_chars", 1200) or 1200)
        spoken = prepare_for_speech(text, max_chars=limit)
        if not spoken:
            return VoiceResult(ok=False, engine=plan.engine, detail="nothing to say")

        async with self._lock:  # one utterance at a time; overlapping speech is unintelligible
            try:
                await asyncio.wait_for(self._run(plan, spoken), timeout=SPEAK_TIMEOUT_SECONDS)
            except TimeoutError:
                self._failures += 1
                return VoiceResult(
                    ok=False,
                    engine=plan.engine,
                    detail=f"the speech engine did not finish within {SPEAK_TIMEOUT_SECONDS}s",
                )
            except Exception as exc:  # noqa: BLE001 - speech must never break a request
                self._failures += 1
                logger.info("speech failed (%s)", exc)
                return VoiceResult(ok=False, engine=plan.engine, detail=str(exc)[:300])

        self._spoken += 1
        result = VoiceResult(
            ok=True,
            engine=plan.engine,
            spoken_chars=len(spoken),
            detail="spoken",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        self._last = result
        return result

    async def _run(self, plan: VoicePlan, text: str) -> None:
        """Launch the engine with an argv (never a shell) and read the text from a temp file."""
        path = self._spool(text)
        try:
            argv = [*plan.argv, str(path)]
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(
                    f"{plan.engine} exited {process.returncode}: "
                    f"{(stderr or b'').decode('utf-8', 'replace').strip()[:200]}"
                )
        finally:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)

    @staticmethod
    def _spool(text: str) -> Path:
        """Write to a UTF-8 temp file. Nothing user- or model-authored reaches a command line."""
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", prefix="dobot-speech-"
        )
        with handle:
            handle.write(text)
        return Path(handle.name)

    def summarise(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.settings, "voice_enabled", False)),
            "engine": self.plan.engine,
            "available": self.available,
            "detail": self.plan.detail,
            "spoken": self._spoken,
            "failures": self._failures,
            "last": self._last.as_dict() if self._last else None,
        }


def build_voice(settings: Any) -> Voice:
    return Voice(settings)
