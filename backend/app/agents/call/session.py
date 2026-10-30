"""One live call: the whole cascade, stage by stage, each streaming into the next.

The shape follows huggingface/speech-to-speech: VAD decides the turn, STT writes it, the LLM
answers incrementally, TTS speaks each sentence as soon as that sentence is complete, and new
speech during any of that cancels it — barge-in, the ``turn_detected`` path of the reference
pipeline. What is deliberately not borrowed is its model stack: STT is Dobot's own local Whisper
(the checkpoint the mic button already uses), the LLM is the NVIDIA Nemotron reasoner the agent
plans with — streamed, thinking off, because a thinking trace before the first spoken word is the
single biggest latency tax in this loop — and TTS is Sarvam's bulbul voices. Every credential
already belongs to this app, and nothing model-shaped has to be installed twice.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import tempfile
import wave
from typing import Any

import httpx
import numpy as np

from app.agents.call.tts import SarvamTts, TtsError
from app.agents.call.vad import SAMPLE_RATE, TurnDetector, VadDetector
from app.config import Settings, get_settings
from app.logging_setup import get_logger

logger = get_logger(__name__)

#: How the model sounds out loud. Every clause here is a latency or legibility decision: no
#: markdown (it would be read as "asterisk asterisk"), short turns (a call is a conversation), and
#: no deep planning (the chat window is where thinking belongs).
CALL_SYSTEM = (
    "You are Dobot, an always-on personal AI assistant, speaking with your user on a voice call. "
    "Answer the way a sharp, friendly colleague talks: plain spoken sentences — no markdown, no "
    "lists, no code, no emojis, none of it is displayed and all of it would be read aloud. Keep "
    "replies to one to three sentences unless the user clearly wants depth. Use natural "
    "contractions, and ask at most one question at a time. If the user asks you to do something "
    "on their computer, say in one sentence what you would do and that they should confirm it in "
    "the chat window — you cannot perform actions from a call. Never read out URLs, tokens or "
    "credentials."
)

#: Enders that may be spoken. A boundary only counts when the ender is followed by a break, so
#: "3.5" and "U.S.A." do not shatter into pieces.
_SENTENCE_END = ".!?…。！？"
_CLOSERS = "\"')]’”»"
#: A model that forgets its punctuation still has to be heard: force a cut at this length.
MAX_SENTENCE_CHARS = 180
#: Conversation memory kept inside the call — enough to refer back, small enough to stay fast.
HISTORY_LIMIT = 16


def take_sentences(buffer: str) -> tuple[list[str], str]:
    """Every complete spoken sentence in ``buffer``, plus what is left of it.

    Punctuation ends a sentence; a newline ends one; and a long unpunctuated run is cut at its
    last word boundary — all so TTS can start on sentence one while the model is still writing
    sentence three.
    """
    sentences: list[str] = []
    rest = buffer.strip("\n")
    while rest:
        cut = -1
        for index, char in enumerate(rest):
            if char not in _SENTENCE_END and char != "\n":
                continue
            tail = index + 1
            while tail < len(rest) and rest[tail] in _CLOSERS:
                tail += 1
            if tail >= len(rest) or rest[tail].isspace():
                cut = tail
                break
        if cut == -1 and len(rest) > MAX_SENTENCE_CHARS:
            window = rest[:MAX_SENTENCE_CHARS]
            cut = max(window.rfind(" "), window.rfind(","), window.rfind(";"))
            if cut < 40:
                cut = MAX_SENTENCE_CHARS
        if cut == -1:
            break
        piece = rest[:cut].strip()
        rest = rest[cut:].lstrip()
        if piece:
            sentences.append(piece)
    return sentences, rest


def resolve_llm(settings: Settings | None = None) -> dict[str, str]:
    """``{base_url, api_key, model}`` for the call's answers.

    The default is the reasoning stack — the NVIDIA Nemotron models Dobot already plans with — so
    a working ``NEBIUS_API_KEY`` alone makes the call talk. An explicitly configured endpoint gets
    *only* the key meant for it: pointing the call at NVIDIA's endpoint must never smuggle the
    Nebius credential across to a third party.
    """
    settings = settings or get_settings()
    model = (settings.voice_llm_model or "").strip() or settings.nemotron_super_model
    base = (settings.voice_llm_base_url or "").strip()
    if base:
        return {
            "base_url": base.rstrip("/"),
            "api_key": (settings.voice_llm_api_key or "").strip(),
            "model": model,
        }
    return {
        "base_url": settings.nebius_base_url.rstrip("/"),
        "api_key": settings.nebius_api_key.strip(),
        "model": model,
    }


def _resample(pcm: bytes, from_rate: int, to_rate: int = SAMPLE_RATE) -> bytes:
    """Whatever the microphone's clock was, to the one rate every stage below expects."""
    if from_rate == to_rate or from_rate <= 0:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16)
    if samples.size == 0:
        return b""
    out_count = max(1, int(samples.size * to_rate / from_rate))
    positions = np.arange(out_count, dtype=np.float64) * (from_rate / to_rate)
    stretched = np.interp(positions, np.arange(samples.size, dtype=np.float64), samples)
    return np.clip(stretched, -32768, 32767).astype(np.int16).tobytes()


class VoiceCallSession:
    """The per-connection half of the call: intake on one side, streamed events on the other."""

    def __init__(self, settings: Settings | None = None, detector: VadDetector | None = None) -> None:
        from app.agents.call.vad import build_detector

        self.settings = settings or get_settings()
        self.vad = TurnDetector(detector or build_detector(self.settings), self.settings)
        self.tts = SarvamTts(self.settings)
        #: Events for the panel, drained by the route's sender task.
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        #: Utterances waiting for an answer; newest wins when both slots are taken.
        self.turns: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        self.history: list[dict[str, str]] = []
        self._gen = 0
        self._cancel = asyncio.Event()
        self._closed = False
        self._answering = False
        self._state = ""
        self._reported: set[str] = set()
        self._reply_text = ""
        self._turn_task: asyncio.Task[None] | None = None
        self._audio_drop_reported = False

    # ------------------------------------------------------------------ outbound

    def emit(self, message: dict[str, Any]) -> None:
        if self._closed:
            return
        if message.get("type") == "call_audio" and self.outbox.qsize() > 4096:
            if not self._audio_drop_reported:
                self._audio_drop_reported = True
                self.report_error(
                    "audio_queue",
                    "The call output was too slow to deliver safely. Press interrupt and try again.",
                )
            return
        self.outbox.put_nowait(message)

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.emit({"type": "call_state", "state": state})

    def report_error(self, code: str, message: str) -> None:
        """Each distinct failure once per call — a flapping retry loop must not shout."""
        if code in self._reported:
            return
        self._reported.add(code)
        logger.warning("voice call: %s — %s", code, message)
        self.emit({"type": "call_error", "code": code, "message": message})

    # ------------------------------------------------------------------ intake

    def feed_audio(self, pcm: bytes, rate: int) -> None:
        if self._closed or not pcm:
            return
        for update in self.vad.feed(_resample(pcm, rate)):
            if update.started:
                self._on_speech_started()
            if update.turn is not None:
                self._offer(update.turn)

    def _on_speech_started(self) -> None:
        if self._answering:
            # The user talked over the reply: cancel the LLM stream and the TTS request now,
            # not when their sentence ends — the ear notices the difference.
            self.barge_in()
        else:
            self.set_state("listening")

    def _offer(self, turn: bytes) -> None:
        try:
            self.turns.put_nowait(turn)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.turns.get_nowait()
            self.turns.put_nowait(turn)
        if not self._answering:
            self.set_state("thinking")

    def barge_in(self) -> None:
        """Stop generating, stop speaking, forget queued turns: the floor is the user's."""
        self._gen += 1
        self._cancel.set()
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        self._cancel = asyncio.Event()
        while True:
            try:
                queued = self.turns.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is None:
                self.turns.put_nowait(None)
                break
        self._answering = False
        self.emit({"type": "call_interrupted"})
        self.set_state("listening")

    # ------------------------------------------------------------------ the loop

    async def answer_loop(self) -> None:
        while not self._closed:
            turn = await self.turns.get()
            if turn is None:
                continue
            self._turn_task = asyncio.create_task(self._handle_turn(turn))
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if self._closed:
                    raise
            finally:
                self._turn_task = None

    async def _handle_turn(self, turn: bytes) -> None:
        gen = self._gen
        self._answering = True
        self._reply_text = ""
        try:
            self.set_state("thinking")
            text = await self._transcribe(turn)
            if self._gen != gen or self._closed:
                return  # barge-in landed mid-transcription: this answer is already stale
            if not text:
                self.set_state("listening")
                return
            self.history.append({"role": "user", "content": text})
            self.history = self.history[-HISTORY_LIMIT:]
            self.emit({"type": "call_transcript", "role": "user", "text": text})
            await self._respond(gen)
            if self._gen == gen and not self._closed and self._reply_text.strip():
                self.history.append({"role": "assistant", "content": self._reply_text.strip()})
                self.history = self.history[-HISTORY_LIMIT:]
        finally:
            self._answering = False
            if self._gen == gen and not self._closed:
                self.set_state("listening")

    async def _transcribe(self, turn: bytes) -> str:
        """The utterance, through the same local Whisper the mic button uses. PCM16 in, text out."""
        fd, path = tempfile.mkstemp(prefix="dobot-call-", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(path, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(SAMPLE_RATE)
                handle.writeframes(turn)
            from app.agents.transcriber import get_transcriber

            result = await get_transcriber().transcribe(path)
            if not result.get("ok"):
                self.report_error("stt", str(result.get("error") or "transcription unavailable"))
                return ""
            return str(result.get("text") or "").strip()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def _respond(self, gen: int) -> None:
        """Stream the answer: text deltas to the captions, sentences to TTS as they close.

        The conversation *is* the request: ``_handle_turn`` puts the question on the tail of
        ``history`` before calling in, so the messages are the system prompt plus that history —
        appending the question again here would send every turn to the model twice.
        """
        endpoint = resolve_llm(self.settings)
        if not endpoint["api_key"]:
            self.report_error(
                "llm",
                "No LLM key for the call — set NEBIUS_API_KEY in .env, or VOICE_LLM_BASE_URL + "
                "VOICE_LLM_API_KEY to use NVIDIA's own endpoint.",
            )
            return
        messages = [{"role": "system", "content": CALL_SYSTEM}, *self.history]
        payload: dict[str, Any] = {
            "model": endpoint["model"],
            "messages": messages,
            "temperature": float(self.settings.voice_llm_temperature),
            "max_tokens": int(self.settings.voice_llm_max_tokens),
            "stream": True,
        }
        if "nemotron" in endpoint["model"].lower():
            # Nemotron's thinking trace spends thousands of tokens before the first word — the
            # dominant latency cost in a spoken turn. The chat window keeps thinking; a call
            # does not.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        buffer = ""
        try:
            async with httpx.AsyncClient(
                base_url=endpoint["base_url"],
                headers={"Authorization": f"Bearer {endpoint['api_key']}"},
                timeout=httpx.Timeout(60.0, connect=10.0),
            ) as client:
                async with client.stream("POST", "/chat/completions", json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")[:400]
                        self.report_error(
                            "llm", f"The answer model refused (HTTP {response.status_code}): {body}"
                        )
                        return
                    async for line in response.aiter_lines():
                        if self._gen != gen or self._closed:
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = event.get("choices") or [{}]
                        delta = (choices[0].get("delta") or {}).get("content")
                        if not isinstance(delta, str) or not delta:
                            continue
                        self._reply_text += delta
                        self.emit({"type": "call_text", "role": "assistant", "delta": delta})
                        buffer += delta
                        ready, buffer = take_sentences(buffer)
                        for sentence in ready:
                            if self._gen != gen or self._closed:
                                return
                            await self._speak(sentence, gen)
        except httpx.HTTPError as exc:
            self.report_error("llm", f"The answer model is unreachable ({exc}).")
            return
        tail = buffer.strip()
        if tail and self._gen == gen and not self._closed:
            await self._speak(tail, gen)

    async def _speak(self, text: str, gen: int) -> None:
        """Synthesise one sentence and stream its samples to the panel while they arrive."""
        if self._gen != gen or self._closed:
            return
        if not self.tts.available:
            self.report_error(
                "tts",
                "SARVAM_API_KEY is not set — the call can listen and answer, but Dobot cannot "
                "speak until the key is in .env.",
            )
            return
        cancel = self._cancel
        self.set_state("speaking")
        try:
            async for pcm in self.tts.synth(text, cancel=cancel):
                if self._gen != gen or self._closed:
                    return
                self.emit(
                    {
                        "type": "call_audio",
                        "rate": int(self.tts.rate),
                        "data": base64.b64encode(pcm).decode("ascii"),
                    }
                )
        except TtsError as exc:
            self.report_error("tts", str(exc))
        except httpx.HTTPError as exc:
            self.report_error("tts", f"Speech synthesis failed ({exc}).")

    # ------------------------------------------------------------------ lifecycle

    def describe(self) -> dict[str, Any]:
        """What the panel shows on connect — each stage's honest status, not a single green tick."""
        from app.agents.transcriber import get_transcriber

        endpoint = resolve_llm(self.settings)
        stt = get_transcriber().probe()
        return {
            "vad": self.vad.describe(),
            "stt": {"state": stt.get("state"), "model": stt.get("model")},
            "tts": {
                "provider": "sarvam",
                "available": self.tts.available,
                "model": self.settings.sarvam_tts_model,
                "voice": self.settings.sarvam_tts_voice.strip() or "default",
                "rate": int(self.tts.rate),
            },
            "llm": {"model": endpoint["model"], "configured": bool(endpoint["api_key"])},
        }

    def status_hints(self) -> list[str]:
        """Plain sentences for anything that will not work, shown before the user says a word."""
        hints: list[str] = []
        from app.agents.transcriber import get_transcriber

        probe = get_transcriber().probe()
        if probe.get("state") != "live":
            hints.append(
                f"Speech-to-text is {probe.get('state', 'unavailable')}: "
                f"{probe.get('detail', 'install the voice extra (uv sync --all-extras)')}."
            )
        if not self.tts.available:
            hints.append("No SARVAM_API_KEY in .env — Dobot will answer in text only.")
        if not resolve_llm(self.settings)["api_key"]:
            hints.append("No LLM key — set NEBIUS_API_KEY (or VOICE_LLM_BASE_URL + VOICE_LLM_API_KEY).")
        return hints

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gen += 1
        self._cancel.set()
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn_task
        while True:
            try:
                queued = self.turns.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued is None:
                break
        with contextlib.suppress(asyncio.QueueFull):
            self.turns.put_nowait(None)
        await self.tts.aclose()
        self.vad.reset()
