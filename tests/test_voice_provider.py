"""Unit tests for the voice provider packages.

The engine-specific TTS/STT bodies that used to live in the web backend
(`pai_web/actions.py`) now live behind the engine-agnostic provider contract in
two packages:

  • drivers/voice         (local)  — whisper.cpp STT + macOS `say` TTS
  • drivers/voice_cloud   (cloud)  — OpenAI STT + ElevenLabs TTS

These tests cover the relocated request/command shapes. The sources are
canonical in this repo, so they import directly (pre-install), matching the
other driver tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAI_SRC = ROOT.parent / "pai" / "src"
sys.path[:0] = [str(PAI_SRC), str(ROOT)]


# --- local: drivers/voice/provider.py --------------------------------------

from drivers.voice import provider as local  # noqa: E402


def test_local_synthesize_uses_say_and_afconvert(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[dict] = []

    def fake_run(args, input, text, capture_output, check, timeout):  # noqa: A002
        runs.append({"args": args, "input": input, "timeout": timeout})
        if args[0] == "/usr/bin/say":
            Path(args[2]).write_bytes(b"aiff")
        elif args[0] == "/usr/bin/afconvert":
            Path(args[-1]).write_bytes(b"m4a-bytes")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(
        local.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"say", "afconvert"} else None,
    )
    monkeypatch.setattr(local.subprocess, "run", fake_run)

    data, mime = local.synthesize("hello local", voice_id="ignored", speed=0.8)

    assert (data, mime) == (b"m4a-bytes", "audio/mp4")
    assert runs[0]["args"][0] == "/usr/bin/say"
    assert runs[0]["args"][3:] == ["-f", "-"]
    assert runs[0]["input"] == "hello local"
    assert runs[1]["args"][0] == "/usr/bin/afconvert"
    assert runs[1]["args"][1:5] == ["-f", "m4af", "-d", "aac"]


def test_local_transcribe_transcodes_webm_then_runs_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, capture_output, text):  # noqa: ANN001
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(local.subprocess, "run", fake_run)
    monkeypatch.setattr(local.stt, "transcribe", lambda wav: f"text from {Path(wav).suffix}")

    out = local.transcribe(b"opus-bytes", content_type="audio/webm", filename="clip.webm")

    assert out == "text from .wav"
    # ffmpeg invoked to make 16 kHz mono WAV.
    assert len(calls) == 1
    assert calls[0][0] == "/usr/bin/ffmpeg"
    assert calls[0][4:10] == ["-ar", "16000", "-ac", "1", "-f", "wav"]


def test_local_transcribe_skips_transcode_for_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_run(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("ffmpeg must not run for WAV input")

    monkeypatch.setattr(local.subprocess, "run", fail_run)
    monkeypatch.setattr(local.stt, "transcribe", lambda wav: "wav-text")

    out = local.transcribe(b"RIFFdata", content_type="audio/wav", filename="clip.wav")
    assert out == "wav-text"


def test_local_transcribe_rejects_empty_audio() -> None:
    with pytest.raises(ValueError, match="empty audio"):
        local.transcribe(b"", content_type="audio/webm", filename="clip.webm")


# --- cloud: drivers/voice_cloud/provider.py --------------------------------

requests = pytest.importorskip("requests")  # noqa: E402
from drivers.voice_cloud import provider as cloud  # noqa: E402


def test_cloud_synthesize_posts_to_elevenlabs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Response:
        content = b"mp3-bytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-test")
    monkeypatch.setenv("ELEVENLABS_MODEL_ID", "test-tts-model")
    monkeypatch.setattr(
        cloud.requests,
        "post",
        lambda url, headers, params, json, timeout: captured.update(
            url=url, headers=headers, params=params, json=json, timeout=timeout
        )
        or Response(),
    )

    data, mime = cloud.synthesize("hello", voice_id="voice-test", speed=2.0)

    assert (data, mime) == (b"mp3-bytes", "audio/mpeg")
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voice-test"
    assert captured["headers"] == {"xi-api-key": "eleven-test", "accept": "audio/mpeg"}
    assert captured["params"] == {"output_format": "mp3_44100_128"}
    assert captured["json"] == {
        "text": "hello",
        "model_id": "test-tts-model",
        "voice_settings": {"speed": 1.2},  # clamped to [0.7, 1.2]
    }
    assert captured["timeout"] == 30


def test_cloud_synthesize_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(cloud, "_reload_dotenv", lambda: None)
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        cloud.synthesize("hi")


def test_cloud_transcribe_posts_audio_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"text": "hello from voice"}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_TRANSCRIBE_MODEL", "test-transcribe")
    monkeypatch.delenv("OPENAI_TRANSCRIBE_LANGUAGE", raising=False)
    monkeypatch.delenv("OPENAI_TRANSCRIBE_PROMPT", raising=False)
    monkeypatch.setattr(
        cloud.requests,
        "post",
        lambda url, headers, data, files, timeout: captured.update(
            url=url, headers=headers, data=data, files=files, timeout=timeout
        )
        or Response(),
    )

    text = cloud.transcribe(
        b"audio-bytes", content_type="audio/webm", filename="clip.webm", language="en"
    )

    assert text == "hello from voice"
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"] == {"Authorization": "Bearer sk-test"}
    assert captured["data"] == {
        "model": "test-transcribe",
        "response_format": "json",
        "language": "en",
    }
    assert captured["files"] == {"file": ("clip.webm", b"audio-bytes", "audio/webm")}
    assert captured["timeout"] == 60


def test_cloud_transcribe_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cloud, "_reload_dotenv", lambda: None)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        cloud.transcribe(b"audio", content_type="audio/webm", filename="clip.webm")
