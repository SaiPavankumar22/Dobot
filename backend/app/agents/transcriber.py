"""Speech-to-text — optional, local, honest about being optional.

Model: `Systran/faster-whisper-small` by default, overridable with ``TRANSCRIBER_MODEL``. A 244M
parameter Whisper — the same size class as the small CrisperWhisper 2.0 checkpoint (484 MB of
weights either way) — chosen for the machines this actually runs on: MIT-licensed, published as a
native CTranslate2 build that `faster-whisper` loads directly, and ~250 MB of RAM once quantised
to int8. Voice commands are short utterances; small is the sweet spot, not a 3 GB large-v3 model
that a laptop has to swap around.

CrisperWhisper (verbatim, `[UM]`/`[UH]` fillers) stays one setting away: point
``TRANSCRIBER_MODEL`` at `nyralabs/faster_CrisperWhisper` (~3 GB, CrisperWhisper 1.0) or any other
CTranslate2 build. The `nyralabs/CrisperWhisper2.0_*` repos themselves are transformers
safetensors that faster-whisper cannot read — convert first (`pip install
"crisperwhisper[convert]"`, then the package's converter) and pass the resulting CT2 directory.

Design, matching the rest of Dobot:

* **Local and private.** The model runs in this process; audio never leaves the machine. No key,
  no network (beyond the one-time checkpoint download from Hugging Face).
* **Lazy.** The checkpoint loads on the first transcription, not at boot — the backend starts just
  as fast without it. Loaded once, kept warm.
* **It finds a device that works.** `TRANSCRIBER_DEVICE=auto` tries the GPU and, if the card cannot
  carry the checkpoint (3 GB of weights against a 4 GB laptop GPU ends in `CUDA failed with error
  out of memory`), falls back to the CPU with `int8` instead of reporting voice input as broken.
* **Optional.** Without the `faster-whisper` package (or on a machine without the memory for it) the
  Doctor reports the exact install command and the API degrades with a clear notice. A microphone
  button that lies about being broken is worse than one that says "not installed".
* **Any format you can record.** Browsers hand over webm/ogg/mp3/m4a; ffmpeg decodes whatever the
  model cannot ingest directly. No ffmpeg → WAV/MP3 still work.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

#: Native CTranslate2 build of OpenAI Whisper *small* (244M params, MIT): the same size class as
#: CrisperWhisper 2.0's small checkpoint, a fraction of the 3 GB large-v3 default this used to
#: have, and loadable by faster-whisper with no conversion step.
_DEFAULT_MODEL = "Systran/faster-whisper-small"

#: A `.` with non-space text on both sides is this build's word-boundary marker, not punctuation —
#: verified against real output, where `[UM].Reminded.Me.Tomorrow.` came back with no spaces at all.
#: Decimals are spared because their neighbour is a digit (`3.5` stays `3.5`).
_BOUNDARY_DOT = re.compile(r"(?<=[^\s\d])\.(?=\S)")
_WHITESPACE = re.compile(r"\s+")


def _cuda_dll_dirs() -> list[Path]:
    """CUDA runtime DLLs that ctranslate2 loads by bare name (`cublas64_12.dll`, `cudnn64_9.dll`).

    With a CUDA toolkit installed they are on PATH already. Without one — a laptop with an NVIDIA
    card, which is the common case — the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels drop them
    into `site-packages/nvidia/*/bin`, which is *not* on PATH. A plain `LoadLibrary` inside
    ctranslate2 searches PATH and nowhere else, so that directory list is the whole difference
    between "GPU transcription" and `cublas64_12.dll is not found`.
    """
    if os.name != "nt":
        return []
    try:
        import site

        bases = list(site.getsitepackages())
    except Exception:  # noqa: BLE001 - no site info is not an error worth reporting
        bases = []
    dirs: list[Path] = []
    for base in bases:
        nvidia = Path(base) / "nvidia"
        if nvidia.is_dir():
            dirs.extend(sorted(path for path in nvidia.glob("*/bin") if path.is_dir()))
    return dirs


def ensure_cuda_on_path() -> list[str]:
    """Put the wheel-shipped CUDA DLLs on PATH for this process. Returns the directories added."""
    added: list[str] = []
    current = os.environ.get("PATH", "")
    for directory in _cuda_dll_dirs():
        text = str(directory)
        if text not in current.split(os.pathsep):
            added.append(text)
    if added:
        os.environ["PATH"] = os.pathsep.join([*added, current])
    return added


def _uses_boundary_dots(model_name: str) -> bool:
    """Whether this checkpoint needs the boundary-dot pass: only Nyra's converted CrisperWhisper
    builds mark word boundaries with `.` — a stock Whisper model emits real spaces."""
    return "crisper" in model_name.lower()


def normalise_transcript(text: str, *, boundary_dots: bool = False) -> str:
    """Tidy a transcript; optionally turn the CrisperWhisper boundary dots back into spaces.

    Nyra Health's converted CrisperWhisper checkpoints mark word boundaries with `.` instead of the
    space token the original Whisper vocabulary uses, so a *correct* transcript reads
    `[UM].Reminded.Me.Tomorrow.At.Five.`. Feeding that to a planner is worse than it looks: it is
    one long token-ish run with no word breaks. Only those checkpoints do it, so the pass is
    opt-in (see :func:`_uses_boundary_dots`). A boundary dot is the one with text tight on both
    sides; a sentence-ending dot has nothing after it, and a decimal has a digit before it. Also
    tidies the spacing punctuation leaves behind.
    """
    if not text:
        return ""
    cleaned = _BOUNDARY_DOT.sub(" ", text) if boundary_dots else text
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    # "," and "." should hug the word before them, never float after a space.
    return re.sub(r"\s+([,.;:!?])", r"\1", cleaned)


class Transcriber:
    """Lazy CrisperWhisper loader + transcriber. One instance per process."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings or get_settings()
        self._model: Any = None
        # (model, attempts) currently loaded — changing any of them reloads.
        self._loaded_for: tuple[str, tuple[tuple[str, str], ...]] | None = None
        self._failed: str = ""
        #: Where the loaded model actually ended up, and why that was not the preferred device.
        self._device: str = ""
        self._note: str = ""
        #: Which entry of :meth:`_attempts` produced the live model (inference demotes to the next).
        self._attempt_index: int = 0

    # ------------------------------------------------------------------ capability

    @property
    def model_name(self) -> str:
        """The checkpoint to load: ``TRANSCRIBER_MODEL`` when set, the CT2 default otherwise."""
        return (getattr(self.settings, "transcriber_model", "") or _DEFAULT_MODEL).strip()

    def _attempts(self) -> list[tuple[str, str]]:
        """``(device, compute_type)`` pairs to try, in order.

        ``auto`` means "prefer the GPU, but only if the card can actually carry the checkpoint". A
        3 GB model does not fit in the 4 GB of a laptop RTX 3050 once Windows has taken its share,
        and a CUDA OOM there used to leave voice input permanently broken while the CPU sat idle. So
        an auto load falls back instead of dying. An explicit ``TRANSCRIBER_DEVICE`` is honoured
        literally — if you ask for ``cuda`` you hear about ``cuda`` — because silently ignoring it
        would make the setting a lie.
        """
        device = (self.settings.transcriber_device or "auto").strip().lower()
        compute = (self.settings.transcriber_compute_type or "auto").strip().lower()
        explicit = compute not in ("", "auto", "default")
        if device == "auto":
            if explicit:
                return [("cuda", compute), ("cpu", compute)]
            # lean types on purpose: the GPU attempt must not need the whole card, and int8 is the
            # fastest honest choice for CPU inference of this size.
            return [("cuda", "int8_float16"), ("cpu", "int8")]
        return [(device, compute if explicit else "default")]

    @property
    def package_available(self) -> bool:
        """faster-whisper is the runtime: the checkpoint ships ctranslate2 weights, and the
        compute_type knob below is a faster-whisper parameter."""
        try:
            import faster_whisper  # noqa: F401

            return True
        except Exception:  # noqa: BLE001
            return False

    @property
    def ffmpeg_available(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    def probe(self) -> dict[str, Any]:
        """Honest status for the Doctor: what is configured, installed, loadable."""
        settings = self.settings
        info: dict[str, Any] = {
            "model": self.model_name,
            "device": self._device or settings.transcriber_device or "auto",
            "ffmpeg": self.ffmpeg_available,
            "loaded": self._model is not None,
        }
        if self._note:
            info["note"] = self._note
        if settings.transcriber_enabled:
            if not self.package_available:
                info["state"] = "not_configured"
                info["detail"] = "the whisper package is not installed"
            elif self._failed:
                info["state"] = "broken"
                info["detail"] = self._failed
            else:
                info["state"] = "live"
        else:
            info["state"] = "declined"
            info["detail"] = "TRANSCRIBER_ENABLED is off"
        return info

    def fail_status(self) -> dict[str, Any]:
        """The /health-shaped status for this capability."""
        if not self.settings.transcriber_enabled or not self.package_available:
            return "not_configured"
        return "degraded" if self._failed else "ok"

    # ------------------------------------------------------------------ model

    def _load(self, skip: int = 0) -> Any:
        """Load the model once. Returns the model or None; never raises.

        ``skip`` ignores the first N candidates — how a completed load that cannot *run* (the classic
        case: the GPU holds the weights, then inference dies with `cublas64_12.dll is not found`)
        gets demoted to the next device without restarting the process.
        """
        if self._model is not None and skip == 0:
            return self._model
        if not self.package_available:
            self._failed = self._failed or "the whisper package is not installed"
            return None
        all_attempts = self._attempts()
        attempts = all_attempts[skip:]
        model_name = self.model_name
        key = (model_name, tuple(all_attempts), skip)
        if self._loaded_for == key and self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            self._failed = f"model load failed: {exc}"[:300]
            return None

        preferred = all_attempts[0][0]
        for index, (device, compute) in enumerate(attempts, start=skip):
            started = time.perf_counter()
            try:
                if device == "cuda":
                    ensure_cuda_on_path()
                model = WhisperModel(
                    model_name, device=device, compute_type=compute, cpu_threads=self._cpu_threads()
                )
            except Exception as exc:  # noqa: BLE001 - bad download, OOM, no CUDA runtime…
                self._failed = f"model load failed on {device}: {exc}"[:300]
                logger.warning("transcriber: %s", self._failed)
                continue
            self._model = model
            self._loaded_for = key
            self._failed = ""
            self._device = device
            self._attempt_index = index
            if device != preferred and not self._note:
                self._note = f"{preferred} could not carry the checkpoint — running on {device}"
            elif device == preferred:
                self._note = ""
            logger.info(
                "transcriber: %s loaded in %.1fs (device=%s, compute=%s)",
                model_name,
                time.perf_counter() - started,
                device,
                compute,
            )
            return self._model
        self._model = None
        self._device = ""
        return None

    def _cpu_threads(self) -> int:
        """Threads for CPU inference. faster-whisper asks CTranslate2 for **4** unless told otherwise —
        a laptop with twelve logical cores spent the whole transcription on a third of them."""
        configured = int(getattr(self.settings, "transcriber_cpu_threads", 0) or 0)
        if configured > 0:
            return configured
        return max(2, (os.cpu_count() or 4))

    def _demote(self, reason: str) -> Any:
        """Drop the device that just failed and load the next candidate. None when there is no next."""
        attempts = self._attempts()
        next_index = self._attempt_index + 1
        if self._model is None or next_index >= len(attempts):
            return None
        dropped = attempts[self._attempt_index][0]
        promoted = attempts[next_index][0]
        self._model = None
        self._loaded_for = None
        self._note = (
            f"{dropped} failed during inference ({reason.strip()[:120]}) — continuing on {promoted}"
        )
        logger.warning("transcriber: %s", self._note)
        return self._load(skip=next_index)

    # ------------------------------------------------------------------ transcription

    async def transcribe(self, path: str | Path, *, language: str | None = None) -> dict[str, Any]:
        """Transcribe an audio file. Returns {ok, text, language, duration_ms, error}."""
        started = time.perf_counter()
        audio = Path(path)
        if not audio.exists():
            return {"ok": False, "text": "", "language": "", "duration_ms": 0, "error": "audio file not found"}
        model = self._load()
        if model is None:
            return {
                "ok": False,
                "text": "",
                "language": "",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": self._failed or "transcription unavailable",
            }
        source = audio
        temp_wav: Path | None = None
        try:
            # The model ingests wav/mp3/flac natively; browsers record webm/ogg — decode via ffmpeg
            # when we have to. No ffmpeg → tell the user precisely that.
            if audio.suffix.lower() not in (".wav", ".mp3", ".flac"):
                if not self.ffmpeg_available:
                    return {
                        "ok": False,
                        "text": "",
                        "language": "",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "error": "ffmpeg is required to decode this recording format",
                    }
                import asyncio

                temp_wav = audio.with_suffix(".decoded.wav")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", str(audio), "-ar", "16000", "-ac", "1", str(temp_wav),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await proc.wait() != 0 or not temp_wav.exists():
                    return {
                        "ok": False,
                        "text": "",
                        "language": "",
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "error": "ffmpeg failed to decode the recording",
                    }
                source = temp_wav

            # A device that loads but cannot run: demote once to the next candidate (normally CPU).
            try:
                result = await _run_inference(model, str(source), language)
            except Exception as exc:  # noqa: BLE001
                demoted = self._demote(str(exc))
                if demoted is None:
                    raise
                result = await _run_inference(demoted, str(source), language)
            text = normalise_transcript(
                str(result.get("text", "")), boundary_dots=_uses_boundary_dots(self.model_name)
            )
            return {
                "ok": bool(text),
                "text": text,
                "language": str(result.get("language", "") or ""),
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": "" if text else "no speech detected",
            }
        except Exception as exc:  # noqa: BLE001 - inference failure is a degraded answer
            logger.warning("transcription failed: %s", exc)
            return {
                "ok": False,
                "text": "",
                "language": "",
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "error": str(exc)[:300],
            }
        finally:
            if temp_wav is not None:
                temp_wav.unlink(missing_ok=True)


async def _run_inference(model: Any, path: str, language: str | None) -> dict[str, Any]:
    """The model call is CPU/GPU-bound; run it off the event loop."""
    import asyncio

    loop = asyncio.get_running_loop()

    def _invoke() -> dict[str, Any]:
        kwargs: dict[str, Any] = {"vad_filter": True}
        if language and language.lower() not in ("auto", ""):
            kwargs["language"] = language
        # faster-whisper returns (segments generator, info); materialise the text here, off the loop.
        segments, info = model.transcribe(path, **kwargs)
        return {
            "text": "".join(segment.text for segment in segments),
            "language": getattr(info, "language", "") or "",
        }

    return await loop.run_in_executor(None, _invoke)


_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = Transcriber()
    return _transcriber


def reset_transcriber() -> None:
    global _transcriber
    _transcriber = None
