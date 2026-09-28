"""Transcriber wiring: the right checkpoint, and honest answers when it cannot run.

None of these touch the network or any real checkpoint — the model is either monkeypatched or
never reached. What they protect is the bug that made voice input silently useless: the transcriber
pointed at a transformers-only repo (`nyralabs/CrisperWhisper2.0_small`) whose weights faster-whisper
cannot read, so every recording failed with `Unable to open file 'model.bin'`. The default is now
Whisper small (a native CT2 build, MIT), with CrisperWhisper checkpoints still one setting away.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agents.transcriber import (
    _DEFAULT_MODEL,
    Transcriber,
    ensure_cuda_on_path,
    normalise_transcript,
)
from app.config import Settings


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class _Info:
    language = "en"


class _FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel: records the repo it was asked for."""

    calls: list[tuple[str, dict]] = []
    segments = ["Um, ", "remind me tomorrow ", "at five."]

    def __init__(self, model_name: str, **kwargs: object) -> None:
        _FakeWhisperModel.calls.append((model_name, kwargs))
        self.device = kwargs.get("device")

    def transcribe(self, path: str, **kwargs: object):  # noqa: ANN201 - mirrors faster-whisper
        return (_Segment(text) for text in type(self).segments), _Info()


class _GpuLoadsButCannotRun(_FakeWhisperModel):
    """The real failure mode on this laptop: the card holds the weights, then inference dies."""

    def transcribe(self, path: str, **kwargs: object):  # noqa: ANN201
        if self.device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return super().transcribe(path, **kwargs)


@pytest.fixture()
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> type[_FakeWhisperModel]:
    _FakeWhisperModel.calls = []
    monkeypatch.setattr(Transcriber, "package_available", property(lambda self: True))
    monkeypatch.setattr("faster_whisper.WhisperModel", _FakeWhisperModel)
    return _FakeWhisperModel


def test_default_checkpoint_is_whisper_small(settings: Settings) -> None:
    """The default loads natively in faster-whisper; the CrisperWhisper2.0_* repos do not."""
    assert Transcriber(settings).model_name == _DEFAULT_MODEL
    assert _DEFAULT_MODEL == "Systran/faster-whisper-small"
    assert not _DEFAULT_MODEL.endswith("2.0_small"), "2.0_* repos are transformers-only"


def test_transcriber_model_is_configurable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBER_MODEL", "someone/tiny-ct2-whisper")
    from app.config import reset_settings_cache

    reset_settings_cache()
    live = Settings()
    assert Transcriber(live).model_name == "someone/tiny-ct2-whisper"
    assert Transcriber(live).probe()["model"] == "someone/tiny-ct2-whisper"


async def test_transcribe_joins_segments(settings: Settings, fake_whisper: type[_FakeWhisperModel], tmp_path: Path) -> None:
    audio = tmp_path / "clip.wav"  # .wav skips the ffmpeg decode path entirely
    audio.write_bytes(b"RIFF....")

    result = await Transcriber(settings).transcribe(audio)

    assert result["ok"] is True
    assert result["text"] == "Um, remind me tomorrow at five."
    assert result["language"] == "en"
    assert fake_whisper.calls[0][0] == _DEFAULT_MODEL


async def test_missing_audio_is_an_answer_not_an_exception(settings: Settings, tmp_path: Path) -> None:
    result = await Transcriber(settings).transcribe(tmp_path / "nope.wav")

    assert result["ok"] is False
    assert result["error"] == "audio file not found"


async def test_load_failure_is_reported_not_swallowed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken checkpoint must show up as `broken`/`degraded`, never as a silent empty transcript."""
    monkeypatch.setattr(Transcriber, "package_available", property(lambda self: True))

    def explode(model_name: str, **kwargs: object) -> None:
        raise RuntimeError("Unable to open file 'model.bin'")

    monkeypatch.setattr("faster_whisper.WhisperModel", explode)
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....")
    transcriber = Transcriber(settings)

    result = await transcriber.transcribe(audio)

    assert result["ok"] is False
    assert "model.bin" in result["error"]
    assert transcriber.probe()["state"] == "broken"
    assert transcriber.fail_status() == "degraded"


def test_cuda_wheel_dirs_are_put_on_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ctranslate2 searches PATH for cublas/cudnn; the nvidia-* wheels live outside it."""
    from app.agents import transcriber as module

    cublas = tmp_path / "nvidia" / "cublas" / "bin"
    cudnn = tmp_path / "nvidia" / "cudnn" / "bin"
    cublas.mkdir(parents=True)
    cudnn.mkdir(parents=True)
    monkeypatch.setattr(module, "_cuda_dll_dirs", lambda: [cublas, cudnn])
    monkeypatch.setenv("PATH", "/usr/bin")

    assert ensure_cuda_on_path() == [str(cublas), str(cudnn)]
    assert os.environ["PATH"] == os.pathsep.join([str(cublas), str(cudnn), "/usr/bin"])
    # idempotent: a second load attempt (a GPU-to-CPU demotion reloads) must not stack duplicates
    assert ensure_cuda_on_path() == []
    assert os.environ["PATH"].count(str(cublas)) == 1


def test_cuda_wheel_dirs_are_empty_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents import transcriber as module

    monkeypatch.setattr(module.os, "name", "posix")
    assert module._cuda_dll_dirs() == []


def test_boundary_dots_become_spaces_when_opted_in() -> None:
    """The exact shape Nyra's converted checkpoints produce — one run with no spaces anywhere."""
    raw = "[UM].Reminded.Me.Tomorrow.At.Five.To.Email.The.Research.Team.About.The.Demo."
    assert normalise_transcript(raw, boundary_dots=True) == (
        "[UM] Reminded Me Tomorrow At Five To Email The Research Team About The Demo."
    )


def test_stock_whisper_output_is_left_alone() -> None:
    """Only CrisperWhisper checkpoints mark boundaries with dots — a stock Whisper emits spaces, and
    the pass must stay off there (it would eat legitimate `end.Start` runs as punctuation)."""
    text = "Um, remind me tomorrow at five."
    assert normalise_transcript(text) == text
    assert normalise_transcript(text, boundary_dots=False) == text


def test_normalisation_keeps_decimals_and_sentences() -> None:
    assert normalise_transcript("It costs 3.5 million.") == "It costs 3.5 million."
    assert normalise_transcript("") == ""
    assert normalise_transcript("Done . Next") == "Done. Next"


async def test_gpu_that_cannot_run_demotes_to_cpu(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CUDA inference failure must cost one retry, not the whole feature."""
    _FakeWhisperModel.calls = []
    monkeypatch.setattr(Transcriber, "package_available", property(lambda self: True))
    monkeypatch.setattr("faster_whisper.WhisperModel", _GpuLoadsButCannotRun)
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....")
    transcriber = Transcriber(settings)

    result = await transcriber.transcribe(audio)

    assert result["ok"] is True
    assert [call[1]["device"] for call in _FakeWhisperModel.calls] == ["cuda", "cpu"]
    assert transcriber.probe()["device"] == "cpu"
    assert "cublas64_12.dll" in transcriber.probe()["note"]
    # and it stays on CPU: no new load attempt is made for the next recording
    before = len(_FakeWhisperModel.calls)
    await transcriber.transcribe(audio)
    assert len(_FakeWhisperModel.calls) == before


def test_explicit_device_is_honoured_not_overridden(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """`TRANSCRIBER_DEVICE=cuda` must mean cuda — no silent CPU that makes the setting a lie."""
    monkeypatch.setenv("TRANSCRIBER_DEVICE", "cuda")
    from app.config import reset_settings_cache

    reset_settings_cache()
    assert Transcriber(Settings())._attempts() == [("cuda", "default")]
    monkeypatch.setenv("TRANSCRIBER_DEVICE", "cpu")
    reset_settings_cache()
    assert Transcriber(Settings())._attempts() == [("cpu", "default")]


def test_cpu_inference_uses_every_core(settings: Settings) -> None:
    """faster-whisper defaults to 4 threads; a 12-core laptop was using a third of itself."""
    assert Transcriber(settings)._cpu_threads() == max(2, os.cpu_count() or 4)

    class _Pinned(Settings):
        transcriber_cpu_threads: int = 3

    assert Transcriber(_Pinned(dobot_state_dir=str(settings.dobot_state_dir)))._cpu_threads() == 3


async def test_crisper_checkpoint_gets_the_boundary_dot_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pointing TRANSCRIBER_MODEL at a CrisperWhisper build re-enables the dot normalisation."""
    monkeypatch.setenv("TRANSCRIBER_MODEL", "nyralabs/faster_CrisperWhisper")
    from app.config import reset_settings_cache

    reset_settings_cache()
    monkeypatch.setattr(Transcriber, "package_available", property(lambda self: True))
    monkeypatch.setattr("faster_whisper.WhisperModel", _FakeWhisperModel)
    _FakeWhisperModel.calls = []
    _FakeWhisperModel.segments = ["[UM].", "Reminded.Me.Tomorrow.", "At.Five."]
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF....")

    result = await Transcriber(Settings()).transcribe(audio)

    assert result["text"] == "[UM] Reminded Me Tomorrow At Five."
    _FakeWhisperModel.segments = ["Um, ", "remind me tomorrow ", "at five."]


def test_probe_states(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Transcriber(settings).probe()["state"] == "live"

    monkeypatch.setenv("TRANSCRIBER_ENABLED", "false")
    from app.config import reset_settings_cache

    reset_settings_cache()
    declined = Transcriber(Settings()).probe()
    assert declined["state"] == "declined"
    assert "TRANSCRIBER_ENABLED" in declined["detail"]

    monkeypatch.setattr(Transcriber, "package_available", property(lambda self: False))
    missing = Transcriber(settings).probe()
    assert missing["state"] == "not_configured"
    assert missing["detail"] == "the whisper package is not installed"
    assert Transcriber(settings).fail_status() == "not_configured"


async def test_non_wav_without_ffmpeg_says_so(
    settings: Settings,
    fake_whisper: type[_FakeWhisperModel],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A browser webm needs ffmpeg; without it the answer names the dependency."""
    monkeypatch.setattr(Transcriber, "ffmpeg_available", property(lambda self: False))
    audio = tmp_path / "clip.webm"
    audio.write_bytes(b"\x1aE\xdf\xa3")

    result = await Transcriber(settings).transcribe(audio)

    assert result["ok"] is False
    assert "ffmpeg" in result["error"]
