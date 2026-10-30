"""The voice call: endpointing, WAV streaming, sentence cuts, credential routing, and the gate.

No network and no model weights: the detector here is a script, the WAVs are built in memory,
and the endpoint that would answer is never dialled. What is protected is the logic that decides
*when* to spend money and *where* credentials go — plus the honest refusals when a stage is off.
"""

from __future__ import annotations

import base64
import io
import threading
import types
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.agents.call.session import VoiceCallSession, _resample, resolve_llm, take_sentences
from app.agents.call.tts import TtsError, WavPcmStream
from app.agents.call.vad import CHUNK_SAMPLES, SAMPLE_RATE, TurnDetector, VadDetector, VadUpdate
from app.config import Settings
from app.main import create_app

# One detector chunk is 512 samples at 16 kHz = 32 ms.
CHUNK_MS = CHUNK_SAMPLES * 1000 // SAMPLE_RATE
CHUNK_BYTES = CHUNK_SAMPLES * 2


class ScriptedDetector(VadDetector):
    """Plays back a speech/no-speech script, repeating its last entry when the script runs out."""

    name = "scripted"

    def __init__(self, voiced: list[bool]) -> None:
        self.voiced = voiced
        self.index = 0

    def is_speech(self, chunk) -> bool:  # noqa: ANN001 - mirrors the real detectors
        value = self.voiced[min(self.index, len(self.voiced) - 1)]
        self.index += 1
        return value


def _silence(ms: int) -> bytes:
    return bytes(CHUNK_BYTES * (ms // CHUNK_MS))


def _settings(**overrides) -> Settings:
    values = {
        "dobot_state_dir": ".dobot-test",
        "nebius_api_key": "",
        "tavily_api_key": "",
        "novita_api_key": "",
        "sarvam_api_key": "",
        "voice_llm_base_url": "",
        "voice_llm_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


# --------------------------------------------------------------------------- turn detection


class TestTurnDetector:
    def test_silence_alone_never_starts_a_turn(self) -> None:
        detector = TurnDetector(ScriptedDetector([False]), _settings())
        updates: list[VadUpdate] = []
        for _ in range(40):
            updates += detector.feed(_silence(CHUNK_MS * 10))
        assert updates == []
        assert not detector.speaking

    def test_a_complete_utterance_carries_its_pad_and_ends(self) -> None:
        # 100 ms quiet (becomes the pad) → 480 ms of speech → silence until the turn closes.
        detector = TurnDetector(
            ScriptedDetector([False] * 3 + [True] * 15 + [False] * 30), _settings()
        )
        pad = _silence(CHUNK_MS * 3)
        updates: list[VadUpdate] = []
        for frame in (pad, _silence(CHUNK_MS * 20), _silence(CHUNK_MS * 40)):
            updates += detector.feed(frame)
        started = [u for u in updates if u.started]
        ended = [u for u in updates if u.turn is not None]
        assert len(started) == 1
        assert len(ended) == 1
        # The pre-speech audio rides with the turn: the first consonant is never clipped.
        assert ended[0].turn is not None and ended[0].turn.startswith(pad)
        assert not detector.speaking

    def test_a_single_voiced_chunk_is_a_click_not_a_turn(self) -> None:
        # One 32 ms blip: below START_RUN_CHUNKS it never starts, and a burst shorter than
        # MIN_TURN_MS starts but is dropped at the end.
        detector = TurnDetector(ScriptedDetector([False, True, False] + [False] * 40), _settings())
        updates: list[VadUpdate] = []
        for _ in range(6):
            updates += detector.feed(_silence(CHUNK_MS * 10))
        assert not any(u.started for u in updates)
        assert not any(u.turn is not None for u in updates)

    def test_short_burst_that_starts_is_dropped_before_the_answer_loop(self) -> None:
        # 3 voiced chunks (96 ms) with a valid two-chunk start: starts, then is too short to ask
        # a model about.
        detector = TurnDetector(ScriptedDetector([False, True, True, True] + [False] * 40), _settings())
        updates: list[VadUpdate] = []
        for _ in range(6):
            updates += detector.feed(_silence(CHUNK_MS * 10))
        assert any(u.started for u in updates)
        assert not any(u.turn is not None for u in updates)


# ------------------------------------------------------------------------- wav stream parsing


def _make_wav(rate: int, frames: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


class TestWavPcmStream:
    @pytest.mark.parametrize("split", [1, 7, 44, 45, 100])
    def test_stream_survives_being_split_anywhere(self, split: int) -> None:
        pcm = bytes(range(256)) * 4
        blob = _make_wav(22050, pcm)
        parser = WavPcmStream(fallback_rate=24000)
        out = b""
        for index in range(0, len(blob), split):
            out += b"".join(parser.feed(blob[index : index + split]))
        out += parser.flush()
        assert parser.rate == 22050  # the header's word wins over the configured guess
        assert out == pcm

    def test_a_response_that_is_not_wav_is_rejected_loudly(self) -> None:
        parser = WavPcmStream(24000)
        with pytest.raises(TtsError):
            parser.feed(b"ID3\x03\x04not audio at all")

    def test_configured_rate_stands_until_the_header_says_otherwise(self) -> None:
        parser = WavPcmStream(24000)
        assert parser.rate == 24000
        parser.feed(_make_wav(16000, b"\x01\x02" * 64))
        assert parser.rate == 16000


# ------------------------------------------------------------------------- sentence cutting


class TestTakeSentences:
    def test_complete_sentences_come_early_and_partial_stays(self) -> None:
        ready, rest = take_sentences("Hello there. How are")
        assert ready == ["Hello there."]
        assert rest == "How are"

    def test_a_second_sentence_lands_in_the_same_pass(self) -> None:
        ready, rest = take_sentences("One. Two? Three")
        assert ready == ["One.", "Two?"]
        assert rest == "Three"

    def test_abbreviations_do_not_shatter(self) -> None:
        # The dot in "3.5" is never a boundary (no break follows it), and a trailing abbreviation
        # closes only at the very end — internal dots must not cut a token in half.
        ready, rest = take_sentences("The version 3.5 is out")
        assert ready == []
        assert rest == "The version 3.5 is out"
        ready, rest = take_sentences("She joined the U.S.A.")
        assert ready == ["She joined the U.S.A."]
        assert rest == ""

    def test_a_run_without_punctuation_is_cut_at_a_word_boundary(self) -> None:
        long = "word " * 60  # 300 chars, no punctuation
        ready, rest = take_sentences(long)
        assert len(ready) == 1
        assert ready[0].endswith("word")  # cut cleanly at a boundary, never mid-word
        assert rest.startswith("word")
        assert len(rest) < len(long)

    def test_closing_quotes_stay_with_their_sentence(self) -> None:
        ready, rest = take_sentences('He said "stop." Then')
        assert ready == ['He said "stop."']
        assert rest == "Then"


# ------------------------------------------------------------------------ credential routing


class TestResolveLlm:
    def test_default_uses_the_reasoning_stack(self) -> None:
        settings = _settings(nebius_api_key="nebius-secret")
        endpoint = resolve_llm(settings)
        assert endpoint["api_key"] == "nebius-secret"
        assert endpoint["model"] == settings.nemotron_super_model

    def test_a_foreign_endpoint_never_receives_the_nebius_key(self) -> None:
        # Pointing the call at NVIDIA must not smuggle the Nebius credential across the wire.
        settings = _settings(
            nebius_api_key="nebius-secret",
            voice_llm_base_url="https://integrate.api.nvidia.com/v1",
            voice_llm_api_key="",
        )
        endpoint = resolve_llm(settings)
        assert endpoint["base_url"] == "https://integrate.api.nvidia.com/v1"
        assert endpoint["api_key"] == ""

    def test_an_explicit_voice_key_and_model_win(self) -> None:
        settings = _settings(
            voice_llm_base_url="https://integrate.api.nvidia.com/v1/",
            voice_llm_api_key="nvidia-secret",
            voice_llm_model="nvidia/nemotron-3-super-120b-a12b",
        )
        endpoint = resolve_llm(settings)
        assert endpoint == {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "nvidia-secret",
            "model": "nvidia/nemotron-3-super-120b-a12b",
        }


# ----------------------------------------------------------------------------- session intake


class TestSessionIntake:
    @pytest.mark.asyncio
    async def test_a_finished_turn_is_queued_and_states_are_announced(self) -> None:
        settings = _settings()
        session = VoiceCallSession(settings, ScriptedDetector([False] * 3 + [True] * 15 + [False] * 30))
        # Two frames large enough to walk the script end to end (pad → speech → trailing quiet).
        session.feed_audio(_silence(CHUNK_MS * 100), rate=SAMPLE_RATE)
        assert not session.turns.empty()
        states = [m["state"] for m in _drained(session) if m["type"] == "call_state"]
        assert "listening" in states or "thinking" in states

    @pytest.mark.asyncio
    async def test_speech_over_a_running_answer_interrupts_it(self) -> None:
        settings = _settings()
        session = VoiceCallSession(
            settings, ScriptedDetector([False] * 4 + [True] * 2)
        )
        # Four quiet chunks (the pad), then two voiced: enough to raise a speech start.
        session._answering = True  # noqa: SLF001 - the flag under test, set as the answer loop would
        session.feed_audio(_silence(CHUNK_MS * 4) + _pcm_of(CHUNK_MS * 2, voiced=True), rate=SAMPLE_RATE)
        messages = _drained(session)
        assert any(m["type"] == "call_interrupted" for m in messages)
        states = [m["state"] for m in messages if m["type"] == "call_state"]
        assert states and states[-1] == "listening"
        assert not session._answering  # noqa: SLF001

    def test_resample_changes_length_with_the_rate(self) -> None:
        source = b"\x01\x00" * 4800  # 100 ms at 48 kHz
        converted = _resample(source, 48000, 16000)
        assert len(converted) == len(source) // 3
        assert _resample(source, 16000, 16000) == source


def _pcm_of(ms: int, *, voiced: bool) -> bytes:
    """Deterministic PCM16: a tone when voiced, zeros when not."""
    import math
    import struct

    count = SAMPLE_RATE * ms // 1000
    if not voiced:
        return bytes(count * 2)
    return struct.pack(f"<{count}h", *(int(8000 * math.sin(i / 5)) for i in range(count)))


def _drained(session: VoiceCallSession) -> list[dict]:
    messages = []
    while not session.outbox.empty():
        messages.append(session.outbox.get_nowait())
    return messages


# -------------------------------------------------------------------------------- ws surface


@pytest.fixture()
def call_client(tmp_path, monkeypatch):
    # No model downloads and no developer credentials in tests.
    monkeypatch.setattr("app.agents.call.vad.ensure_silero_model", lambda settings=None: None)
    monkeypatch.setattr("app.security.local_secrets._keyring", lambda: None)
    monkeypatch.setattr("app.config.REPO_ROOT", tmp_path)
    settings = Settings(
        dobot_state_dir=str(tmp_path / ".dobot"),
        transcriber_enabled=False,
        nebius_api_key="",
        sarvam_api_key="",
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


class TestCallSocket:
    def test_open_backend_accepts_and_reports_each_stage(self, call_client) -> None:
        client, _settings_ = call_client
        with client.websocket_connect("/voice/call") as stream:
            loading = stream.receive_json()
            assert loading == {"type": "call_state", "state": "loading"}
            status = stream.receive_json()
            assert status["type"] == "call_status"
            assert status["vad"]["detector"] == "energy"  # silero is not fetched in tests
            assert status["tts"]["provider"] == "sarvam"
            assert status["llm"]["configured"] is False
            stream.send_json({"type": "end"})

    def test_token_gated_backend_refuses_then_accepts(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("app.agents.call.vad.ensure_silero_model", lambda settings=None: None)
        monkeypatch.setattr("app.security.local_secrets._keyring", lambda: None)
        monkeypatch.setattr("app.config.REPO_ROOT", tmp_path)
        settings = Settings(
            dobot_state_dir=str(tmp_path / ".dobot"),
            dobot_api_token="sekrit",
            nebius_api_key="",
            sarvam_api_key="",
        )
        with TestClient(create_app(settings)) as guarded:
            with pytest.raises(WebSocketDisconnect) as refused:
                with guarded.websocket_connect("/voice/call") as stream:
                    stream.receive_json()
            assert refused.value.code == 4401

            with guarded.websocket_connect("/voice/call?access_token=sekrit") as stream:
                assert stream.receive_json() == {"type": "call_state", "state": "loading"}
                assert stream.receive_json()["type"] == "call_status"
                stream.send_json({"type": "end"})

    def test_disabled_calls_say_so_instead_of_silently_failing(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("app.agents.call.vad.ensure_silero_model", lambda settings=None: None)
        monkeypatch.setattr("app.security.local_secrets._keyring", lambda: None)
        monkeypatch.setattr("app.config.REPO_ROOT", tmp_path)
        settings = Settings(
            dobot_state_dir=str(tmp_path / ".dobot"),
            voice_call_enabled=False,
            nebius_api_key="",
            sarvam_api_key="",
        )
        with TestClient(create_app(settings)) as off:
            with pytest.raises(WebSocketDisconnect) as closed:
                with off.websocket_connect("/voice/call") as stream:
                    message = stream.receive_json()
                    assert message["type"] == "call_error"
                    assert message["code"] == "disabled"
                    stream.receive_json()
            assert closed.value.code == 4404


# --------------------------------------------------------------- the whole pipeline, on the wire
#
# One spoken turn, end to end: mic frames in, a transcript, a streamed answer and spoken audio
# back out. Only the three stages that would cost money or need weights are stood in for — local
# Whisper, the answer model's HTTP stream, and Sarvam's synthesis. The route, the gate, the real
# energy detector, the session state machine, the sentence cuts and the wire protocol are exactly
# what ships.

class _FakeTranscriber:
    """Local Whisper's stand-in: one canned transcript, and a probe that admits it is live."""

    async def transcribe(self, path, language=None):  # noqa: ANN001 - mirrors the real signature
        return {"ok": True, "text": "hello dobot", "language": "en", "duration_ms": 5}

    def probe(self) -> dict:
        return {"state": "live", "model": "fake-whisper", "detail": ""}

    def warm(self) -> dict:
        return self.probe()


class _FakeSynth:
    """Sarvam's stand-in: a real async generator of PCM16, so the audio path is exercised."""

    def __init__(self, settings) -> None:  # noqa: ANN001
        self.settings = settings
        self.rate = 24000
        self.available = True

    async def synth(self, text, cancel=None):  # noqa: ANN001
        yield b"\x01\x00" * 480  # 20 ms of PCM16 at 24 kHz

    async def aclose(self) -> None:
        return None


#: What the call asked the answer model — path, headers and payload — for the assertions to check.
_LLM_SENT: dict = {}


class _AnswerStream:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b"answer model error"


class _AnswerModel:
    """Just enough of httpx.AsyncClient for the call's single streamed endpoint — no socket."""

    def __init__(self, base_url=None, headers=None, timeout=None) -> None:  # noqa: ANN001
        self._headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def stream(self, method, path, json=None):  # noqa: ANN001
        _LLM_SENT.clear()
        _LLM_SENT.update({"method": method, "path": path, "headers": self._headers, "payload": json})
        return _AnswerStream(
            [
                'data: {"choices":[{"delta":{"content":"Hi there. "}}]}',
                'data: {"choices":[{"delta":{"content":"I am listening."}}]}',
                "data: [DONE]",
            ]
        )


def _pump(stream, done, limit: int = 40, timeout: float = 20.0) -> list[dict]:
    """Read replies until ``done(messages)`` holds, off the test thread.

    A stalled pipeline then fails with the messages it *did* produce instead of hanging the run.
    """
    seen: list[dict] = []

    def run() -> None:
        for _ in range(limit):
            if done(seen):
                return
            try:
                seen.append(stream.receive_json())
            except Exception:  # noqa: BLE001 - the socket ended before the answer did
                return

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), f"the pipeline stalled after: {[m.get('type') for m in seen]}"
    return seen


def _answered(seen: list[dict]) -> bool:
    """Spoken audio arrived, *and* the turn has since closed with a final listening state."""
    if not any(m.get("type") == "call_audio" for m in seen):
        return False
    spoken = max(index for index, message in enumerate(seen) if message.get("type") == "call_audio")
    return any(
        message.get("type") == "call_state" and message.get("state") == "listening"
        for message in seen[spoken + 1 :]
    )


@pytest.fixture()
def pipeline_client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agents.call.vad.ensure_silero_model", lambda settings=None: None)
    monkeypatch.setattr("app.security.local_secrets._keyring", lambda: None)
    monkeypatch.setattr("app.config.REPO_ROOT", tmp_path)
    monkeypatch.setattr("app.agents.transcriber.get_transcriber", lambda: _FakeTranscriber())
    monkeypatch.setattr("app.agents.call.session.SarvamTts", _FakeSynth)
    monkeypatch.setattr(
        "app.agents.call.session.httpx",
        types.SimpleNamespace(
            AsyncClient=_AnswerModel,
            Timeout=httpx.Timeout,
            HTTPError=httpx.HTTPError,
        ),
    )
    _LLM_SENT.clear()
    settings = Settings(
        dobot_state_dir=str(tmp_path / ".dobot"),
        transcriber_enabled=False,
        nebius_api_key="",
        sarvam_api_key="",
        voice_llm_base_url="https://voice-llm.test/v1",
        voice_llm_api_key="voice-key",
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


class TestFullPipeline:
    def test_a_spoken_turn_flows_from_the_mic_to_the_speaker(self, pipeline_client) -> None:
        client, settings = pipeline_client
        with client.websocket_connect("/voice/call") as stream:
            assert stream.receive_json() == {"type": "call_state", "state": "loading"}
            status = stream.receive_json()
            assert status["type"] == "call_status"
            assert status["vad"]["detector"] == "energy"  # silero is not fetched in tests
            assert status["tts"]["available"] is True
            assert status["llm"]["configured"] is True
            assert status["hints"] == []  # every stage is honestly live

            # 100 ms of quiet (the pre-speech pad) → 900 ms of "speech" → the silence that closes
            # the turn. Frames ride at 16 kHz, the rate every stage speaks natively.
            for pcm in (_pcm_of(100, voiced=False), _pcm_of(900, voiced=True), _pcm_of(700, voiced=False)):
                stream.send_json(
                    {
                        "type": "audio",
                        "rate": SAMPLE_RATE,
                        "data": base64.b64encode(pcm).decode("ascii"),
                    }
                )

            messages = _pump(stream, _answered)
            states = [m["state"] for m in messages if m["type"] == "call_state"]
            assert "listening" in states  # VAD heard the speech start
            assert "thinking" in states  # the turn went to the model
            assert "speaking" in states  # and came back out loud
            assert states[-1] == "listening"  # and the turn closed cleanly
            assert not any(m["type"] == "call_error" for m in messages)

            transcript = next(m for m in messages if m["type"] == "call_transcript")
            assert transcript["role"] == "user"
            assert transcript["text"] == "hello dobot"

            caption = "".join(m["delta"] for m in messages if m["type"] == "call_text")
            assert caption == "Hi there. I am listening."

            audio = [m for m in messages if m["type"] == "call_audio"]
            assert audio and all(m["rate"] == 24000 for m in audio)
            samples = base64.b64decode(audio[0]["data"])
            assert len(samples) > 0 and len(samples) % 2 == 0  # whole PCM16 frames only

            # The request that answered: the right endpoint's key, thinking off, and the question
            # riding exactly once — history already ends with it when _respond builds the messages.
            assert _LLM_SENT["method"] == "POST"
            assert _LLM_SENT["path"] == "/chat/completions"
            assert _LLM_SENT["headers"]["Authorization"] == "Bearer voice-key"
            payload = _LLM_SENT["payload"]
            assert payload["model"] == settings.nemotron_super_model
            assert payload["stream"] is True
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            assert [message["role"] for message in payload["messages"]] == ["system", "user"]
            assert payload["messages"][-1]["content"] == "hello dobot"

            stream.send_json({"type": "end"})

    def test_the_stop_button_and_a_strange_frame_are_answered_honestly(self, pipeline_client) -> None:
        client, _ = pipeline_client
        with client.websocket_connect("/voice/call") as stream:
            assert stream.receive_json() == {"type": "call_state", "state": "loading"}
            assert stream.receive_json()["type"] == "call_status"

            # The panel's Stop: the floor becomes the user's, even mid-answer. The interrupted
            # event leads, the state change follows it — read past both before judging.
            stream.send_json({"type": "cancel"})

            def stopped(seen: list[dict]) -> bool:
                if not any(m.get("type") == "call_interrupted" for m in seen):
                    return False
                cut = max(i for i, m in enumerate(seen) if m.get("type") == "call_interrupted")
                return any(
                    m.get("type") == "call_state" and m.get("state") == "listening"
                    for m in seen[cut + 1 :]
                )

            messages = _pump(stream, stopped)
            states = [m["state"] for m in messages if m["type"] == "call_state"]
            assert states and states[-1] == "listening"

            # A frame the protocol does not know is refused in words, not by dropping the socket.
            stream.send_json({"type": "reconfigure-the-antenna"})
            messages = _pump(stream, lambda seen: any(m.get("type") == "call_error" for m in seen))
            assert messages[-1]["code"] == "protocol"

            stream.send_json({"type": "end"})
