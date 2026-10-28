"""Endpointing for the voice call: when speech starts, and when it has stopped being spoken.

The detector is Silero v5 — the same default huggingface/speech-to-speech uses — running as a
2.3 MB ONNX model fetched once into the models directory. While ``onnxruntime`` is missing or the
download fails, an adaptive energy detector keeps the same contract, noisier in a loud room; the
call's status line says which one is live instead of pretending they are equal.

Everything here speaks 16 kHz mono PCM16 (the rate Whisper and Silero both want), so no stage
between the microphone and the transcript resamples anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16_000
#: Silero's window at 16 kHz: 512 samples = 32 ms per score.
CHUNK_SAMPLES = 512
#: Consecutive voiced chunks required to call it a start — one chunk is a click, two is a word's
#: onset. 64 ms of certainty, paid once at the beginning of every turn.
START_RUN_CHUNKS = 2
#: A turn this short was a cough or a keypress; it never reaches the model.
MIN_TURN_MS = 350
#: Hard stop: someone who never pauses gets a turn anyway, cut at this length.
MAX_TURN_SECONDS = 45


def ensure_silero_model(settings: Settings | None = None) -> Path | None:
    """Fetch Silero once into the models dir. None when it cannot be had (offline, blocked, tiny).

    Blocking on purpose — the caller runs it in a thread at call setup, and the result is cached
    on disk forever after.
    """
    settings = settings or get_settings()
    target = settings.models_dir / "silero_vad.onnx"
    try:
        if target.exists() and target.stat().st_size > 1_000_000:
            return target
    except OSError:  # unreadable cache: fall through and try a fresh fetch
        pass
    url = (settings.voice_vad_model_url or "").strip()
    if not url:
        return None
    tmp = target.with_suffix(".part")
    try:
        import httpx

        with httpx.stream("GET", url, timeout=30.0, follow_redirects=True) as response:
            response.raise_for_status()
            with open(tmp, "wb") as handle:
                for block in response.iter_bytes(65_536):
                    handle.write(block)
        if tmp.stat().st_size <= 1_000_000:
            tmp.unlink(missing_ok=True)
            return None
        tmp.replace(target)
        logger.info("vad: silero_vad.onnx cached (%d bytes)", target.stat().st_size)
        return target
    except Exception as exc:  # noqa: BLE001 - any fetch failure just means the energy detector
        tmp.unlink(missing_ok=True)
        logger.warning("vad: could not fetch the silero model (%s); falling back to energy", exc)
        return None


class VadDetector:
    """Scores one 512-sample window; True when it sounds like speech."""

    name = ""

    def reset(self) -> None:  # a new call starts with no memory of the last one
        return None

    def is_speech(self, chunk: np.ndarray) -> bool:
        raise NotImplementedError


class SileroDetector(VadDetector):
    """Silero v5 through onnxruntime, with the model's recurrent state carried across chunks."""

    name = "silero"

    def __init__(self, model_path: Path, threshold: float) -> None:
        import onnxruntime as ort

        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._threshold = float(threshold)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def is_speech(self, chunk: np.ndarray) -> bool:
        outputs = self._session.run(
            None,
            {
                "input": chunk.astype(np.float32)[None, :],
                "state": self._state,
                "sr": self._sr,
            },
        )
        # Layout is [speech probability] … [new state]; reading by position survives the naming
        # differences between released model revisions.
        probability = float(np.asarray(outputs[0]).reshape(-1)[0])
        self._state = np.asarray(outputs[-1], dtype=np.float32)
        return probability >= self._threshold


class EnergyDetector(VadDetector):
    """Speech/nothing without a model: an adaptive noise floor with hysteresis.

    Honest about being second-best — a loud keyboard passes for speech in a quiet room, and a
    whisper can hide under a fan — which is exactly why the call reports which detector ran.
    """

    name = "energy"

    def __init__(self) -> None:
        self._floor = 0.01

    def reset(self) -> None:
        self._floor = 0.01

    def is_speech(self, chunk: np.ndarray) -> bool:
        rms = float(np.sqrt(float(np.mean(np.square(chunk, dtype=np.float64)))))
        voiced = rms > max(0.02, self._floor * 3.2)
        if not voiced:
            # The floor follows quiet slowly, so a room's ambience is tracked without ever
            # being dragged up by the speech it is supposed to measure against.
            self._floor = 0.98 * self._floor + 0.02 * max(rms, 1e-4)
        return voiced


def build_detector(settings: Settings | None = None, model_path: Path | None = None) -> VadDetector:
    """Silero when it is installed and fetched; the energy detector otherwise. Never raises."""
    settings = settings or get_settings()
    path = model_path if model_path is not None else ensure_silero_model(settings)
    if path is not None:
        try:
            return SileroDetector(path, settings.voice_vad_threshold)
        except Exception as exc:  # noqa: BLE001 - no onnxruntime, corrupt file, …
            logger.warning("vad: silero unavailable (%s); using the energy detector", exc)
    return EnergyDetector()


class VadUpdate:
    """What one stretch of fed audio decided: speech began, and/or a finished utterance."""

    __slots__ = ("started", "turn")

    def __init__(self, started: bool = False, turn: bytes | None = None) -> None:
        self.started = started
        self.turn = turn


class TurnDetector:
    """Frames in, turns out.

    Accepts PCM16 frames in any size, buffers the detector's 512-sample window, keeps
    ``voice_vad_pad_ms`` of pre-speech audio so the first consonant is never clipped off the
    transcript, and ends a turn after ``voice_vad_silence_ms`` of quiet — long enough that a
    speaker's own mid-sentence pause does not cut them in half.
    """

    def __init__(self, detector: VadDetector, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.detector = detector
        self._carry = bytearray()
        self._pad = bytearray()
        self._speech: bytearray | None = None
        self._voiced_run = 0
        self._quiet_ms = 0.0
        self._voiced_ms = 0.0
        pad_samples = SAMPLE_RATE * self.settings.voice_vad_pad_ms // 1000
        self._pad_bytes = pad_samples * 2

    @property
    def speaking(self) -> bool:
        return self._speech is not None

    def reset(self) -> None:
        self._carry.clear()
        self._pad.clear()
        self._speech = None
        self._voiced_run = 0
        self._quiet_ms = 0.0
        self._voiced_ms = 0.0
        self.detector.reset()

    def feed(self, raw: bytes) -> list[VadUpdate]:
        updates: list[VadUpdate] = []
        if not raw:
            return updates
        self._carry.extend(raw)
        chunk_bytes = CHUNK_SAMPLES * 2
        chunk_ms = CHUNK_SAMPLES * 1000.0 / SAMPLE_RATE
        while len(self._carry) >= chunk_bytes:
            window = bytes(self._carry[:chunk_bytes])
            del self._carry[:chunk_bytes]
            samples = np.frombuffer(window, dtype=np.int16).astype(np.float32) / 32768.0
            voiced = self.detector.is_speech(samples)

            if self._speech is None:
                # Between turns: hold a rolling pad, so the audio that *led up to* the trigger
                # travels with the turn instead of being lost under the detector's latency.
                self._pad.extend(window)
                if len(self._pad) > self._pad_bytes:
                    del self._pad[: len(self._pad) - self._pad_bytes]
                if voiced:
                    self._voiced_run += 1
                    if self._voiced_run >= START_RUN_CHUNKS:
                        self._speech = bytearray(self._pad)
                        self._pad.clear()
                        self._quiet_ms = 0.0
                        # The confirming run *is* speech and counts toward the minimum — otherwise
                        # a short utterance would be judged on what came after its own start.
                        self._voiced_ms = START_RUN_CHUNKS * chunk_ms
                        updates.append(VadUpdate(started=True))
                else:
                    self._voiced_run = 0
                continue

            self._speech.extend(window)
            if voiced:
                self._quiet_ms = 0.0
                self._voiced_ms += chunk_ms
            else:
                self._quiet_ms += chunk_ms
            capped = len(self._speech) >= SAMPLE_RATE * 2 * MAX_TURN_SECONDS
            if capped or self._quiet_ms >= self.settings.voice_vad_silence_ms:
                turn = bytes(self._speech)
                self._speech = None
                self._voiced_run = 0
                self._quiet_ms = 0.0
                # The bar is *spoken* time, not turn length: every turn carries the pre-speech pad
                # and the trailing silence that ended it, so length alone could never tell a word
                # from a keypress. Voiced time can.
                if self._voiced_ms >= MIN_TURN_MS:
                    updates.append(VadUpdate(turn=turn))
                # else: shorter than MIN_TURN_MS — a cough, not an utterance. Discarded here so
                # the transcript slot never shows a turn that never happened.
                self._voiced_ms = 0.0
        return updates

    def describe(self) -> dict[str, Any]:
        return {
            "detector": self.detector.name,
            "sample_rate": SAMPLE_RATE,
            "silence_ms": self.settings.voice_vad_silence_ms,
        }
