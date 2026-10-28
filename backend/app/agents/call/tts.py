"""Sarvam AI (bulbul) text-to-speech for the voice call — streamed, and cancellable.

One HTTP request per sentence against Sarvam's streaming endpoint, so the panel starts talking
while synthesis is still running and the audio is never waited for in full. Cancellation is the
part the reference pipeline flags as best-effort against third-party TTS: here it is exact,
because the call owns the connection — closing it stops the chunks and the barge-in that follows
drops them, exactly like ``turn_detected`` does upstream.

The wire format is a WAV stream: the header is scanned once, and everything after ``data`` is
PCM16 at the rate the header declares (that declared rate, not the configured one, is what the
panel is told to play — a provider default that changes must not become a voice that speaks too
fast).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import Settings, get_settings


class TtsError(Exception):
    """The TTS provider refused or misbehaved in a way the panel should be told about."""


class WavPcmStream:
    """Pulls PCM16 samples out of a WAV stream without waiting for the file to finish.

    A streaming writer leaves the ``data`` size at 0 or 0xffffffff, so sizes are never trusted:
    the header is parsed once for the sample rate, and every byte after ``data`` is audio. An
    odd trailing byte is held back until its partner arrives (streams split at any boundary) and
    a response that is not WAV at all is rejected loudly rather than played as noise.
    """

    def __init__(self, fallback_rate: int) -> None:
        self.rate = fallback_rate
        self._head = bytearray()
        self._started = False
        self._hold = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        if not self._started:
            self._head.extend(chunk)
            if len(self._head) >= 4 and not self._head.startswith(b"RIFF") and not self._head.startswith(
                b"data"
            ):
                raise TtsError(
                    "Sarvam returned an unexpected audio format — the streaming endpoint is "
                    "asked for WAV (output_audio_codec=wav); check SARVAM_TTS_* settings."
                )
            marker = bytes(self._head).find(b"data")
            if marker < 0:
                if len(self._head) > 16_384:
                    raise TtsError("Sarvam's response held no WAV header after 16 kB; not audio.")
                return []
            if marker + 8 > len(self._head):
                return []  # the `data` size field itself has not fully arrived — it is header,
                # not audio, so the parser waits for all of it before treating bytes as samples
            fmt = bytes(self._head).find(b"fmt ")
            if fmt >= 0 and len(self._head) >= fmt + 16:
                declared = int.from_bytes(bytes(self._head[fmt + 12 : fmt + 16]), "little")
                if declared:
                    self.rate = declared
            pcm = bytes(self._head[marker + 8 :])
            self._head.clear()
            self._started = True
            return self._emit(pcm)
        return self._emit(chunk)

    def flush(self) -> bytes:
        tail, self._hold = self._hold, b""
        return tail

    def _emit(self, data: bytes) -> list[bytes]:
        buffer = self._hold + data
        usable = len(buffer) - (len(buffer) % 2)
        if usable == 0:
            self._hold = buffer
            return []
        self._hold = buffer[usable:]
        return [buffer[:usable]]


class SarvamTts:
    """The call's speaking voice: bulbul over Sarvam's streaming endpoint."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        #: The rate the *provider's* header declared, or the configured one before the first byte.
        self.rate = int(self.settings.sarvam_tts_sample_rate)

    @property
    def available(self) -> bool:
        return bool(self.settings.sarvam_api_key.strip())

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.sarvam_tts_base_url.rstrip("/"),
                headers={
                    "api-subscription-key": self.settings.sarvam_api_key.strip(),
                    "Content-Type": "application/json",
                },
                # Connect fast, allow a slow synthesis: TTFA is what matters, not a hard total.
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        return self._client

    def payload(self, text: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "text": text,
            "language_code": self.settings.sarvam_tts_language,
            "model": self.settings.sarvam_tts_model,
            "speech_sample_rate": int(self.settings.sarvam_tts_sample_rate),
            "output_audio_codec": "wav",
        }
        voice = self.settings.sarvam_tts_voice.strip()
        if voice:
            # Empty means the model's own default persona — the field is omitted rather than
            # sent blank, because a blank speaker is a validation error, not "default".
            body["speaker"] = voice
        return body

    async def synth(self, text: str, *, cancel: asyncio.Event) -> AsyncIterator[bytes]:
        """Yield PCM16 for ``text`` as it is synthesised; stop immediately once ``cancel`` is set.

        ``cancel`` is the barge-in channel: the moment the user talks over the reply, the caller
        sets it and the generator closes this request rather than finishing a sentence nobody
        will hear.
        """
        parser = WavPcmStream(int(self.settings.sarvam_tts_sample_rate))
        async with self._http().stream(
            "POST", "/text-to-speech/stream", json=self.payload(text)
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:400]
                raise TtsError(f"Sarvam TTS refused (HTTP {response.status_code}): {body}")
            async for chunk in response.aiter_bytes():
                if cancel.is_set():
                    return
                pieces = parser.feed(chunk)
                # The declared rate lands before the first samples of it are handed on, so the
                # panel never plays a chunk at the wrong speed — even when it differs from config.
                self.rate = parser.rate or self.rate
                for pcm in pieces:
                    yield pcm
            tail = parser.flush()
        if tail:
            yield tail

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
