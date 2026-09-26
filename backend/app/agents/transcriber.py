"""Speech-to-text with CrisperWhisper 2.0 small — optional, local, honest about being optional.

Model: `nyralabs/CrisperWhisper2.0_small` (241 M parameters, ~500 MB in BF16). A Whisper-family
model trained for *verbatim* transcription: it keeps filler words and produces word-level
timestamps, which is exactly what a voice-driven assistant wants ("um, remind me tomorrow… at
five" still resolves to a real reminder). English and German.

Design, matching the rest of Dobot:

* **Local and private.** The model runs in this process; audio never leaves the machine. No key,
  no network (beyond the one-time checkpoint download from Hugging Face).
* **Lazy.** The checkpoint loads on the first transcription, not at boot — the backend starts just
  as fast without it. Loaded once, kept warm.
* **Optional.** Without the `faster-whisper` package (or on a machine without the memory for it) the
  Doctor reports the exact install command and the API degrades with a clear notice. A microphone
  button that lies about being broken is worse than one that says "not installed".
* **Any format you can record.** Browsers hand over webm/ogg/mp3/m4a; ffmpeg decodes whatever the
  model cannot ingest directly. No ffmpeg → WAV/MP3 still work.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

_HF_REPO = "nyralabs/CrisperWhisper2.0_small"


class Transcriber:
    """Lazy CrisperWhisper loader + transcriber. One instance per process."""

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings or get_settings()
        self._model: Any = None
        self._loaded_for: tuple[str, str] | None = None  # (device, compute_type) currently loaded
        self._failed: str = ""

    # ------------------------------------------------------------------ capability

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
            "model": _HF_REPO,
            "device": settings.transcriber_device or "auto",
            "ffmpeg": self.ffmpeg_available,
            "loaded": self._model is not None,
        }
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

    def _load(self) -> Any:
        """Load the model once. Returns the model or None; never raises."""
        if self._model is not None:
            return self._model
        if not self.package_available:
            self._failed = self._failed or "the whisper package is not installed"
            return None
        device = (self.settings.transcriber_device or "auto").strip()
        compute = (self.settings.transcriber_compute_type or "auto").strip()
        key = (device, compute)
        if self._loaded_for == key and self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel

            started = time.perf_counter()
            kwargs: dict[str, Any] = {"device": device}
            if compute and compute != "auto":
                kwargs["compute_type"] = compute
            self._model = WhisperModel(_HF_REPO, **kwargs)
            self._loaded_for = key
            self._failed = ""
            logger.info(
                "transcriber: %s loaded in %.1fs (device=%s)",
                _HF_REPO,
                time.perf_counter() - started,
                device,
            )
            return self._model
        except Exception as exc:  # noqa: BLE001 - bad download, OOM, missing ffmpeg…
            self._failed = f"model load failed: {exc}"[:300]
            logger.warning("transcriber: %s", self._failed)
            return None

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

            result = await _run_inference(model, str(source), language)
            text = str(result.get("text", "")).strip()
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
