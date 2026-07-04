"""Local voice provider — engine-blind surface the web dispatcher calls.

Both functions are the contract every voice package implements (see
`drivers/voice_cloud/provider.py` for the cloud counterpart):

    transcribe(audio, *, content_type, filename, language=None, prompt=None) -> str
    synthesize(text, *, voice_id=None, speed=None) -> (bytes, mime)

Local = fully on-device: whisper.cpp for STT (reusing `stt.transcribe`), macOS
`say` + `afconvert` for TTS. `voice_id`/`speed` are accepted for contract parity
but ignored — `say` leaves voice selection to System Settings.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import stt


# ── STT ──────────────────────────────────────────────────────────────────────

# Browser push-to-talk sends webm/opus (or mp4); whisper.cpp wants 16 kHz mono
# WAV. The host-mic listener already writes WAV (see inbound.py), so transcode
# is only ever hit on the web PTT path.
_WAV_TYPES = {"audio/wav", "audio/x-wav", "audio/wave"}


def _suffix_for(content_type: str, filename: str) -> str:
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    sub = content_type.split("/", 1)[-1].split(";", 1)[0].strip()
    return f".{sub}" if sub else ".bin"


def _to_wav(src: Path, dst: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found; needed to transcode browser audio to WAV "
            "(run `paiman install voice` to provision it)"
        )
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-ar", "16000", "-ac", "1", "-f", "wav", str(dst)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg transcode failed: {proc.stderr.strip()[:300]}")


def transcribe(
    audio: bytes,
    *,
    content_type: str,
    filename: str,
    language: str | None = None,  # noqa: ARG001 — whisper.cpp is pinned to `en`
    prompt: str | None = None,    # noqa: ARG001 — no initial-prompt plumbing yet
) -> str:
    """Turn recorded audio bytes into text via whisper.cpp."""
    if not audio:
        raise ValueError("empty audio")

    with tempfile.TemporaryDirectory(prefix="pai-stt-") as tmp:
        src = Path(tmp) / f"audio{_suffix_for(content_type, filename)}"
        src.write_bytes(audio)
        is_wav = content_type.split(";", 1)[0].strip() in _WAV_TYPES or src.suffix == ".wav"
        if is_wav:
            wav = src
        else:
            wav = Path(tmp) / "audio.wav"
            _to_wav(src, wav)
        return stt.transcribe(wav)


# ── TTS ──────────────────────────────────────────────────────────────────────


def synthesize(
    text: str,
    *,
    voice_id: str | None = None,  # noqa: ARG001 — `say` uses the system voice
    speed: float | None = None,
) -> tuple[bytes, str]:
    """Turn text into playable audio via macOS `say` + `afconvert` (AAC/m4a).

    `voice_id` is ignored — `say` leaves voice selection to System Settings.
    `speed` (a 0.7–1.2 multiplier the web UI sends) maps to `say -r <wpm>` off a
    ~175 wpm baseline, so the read-aloud speed slider works for Siri too.
    """
    say_path = shutil.which("say")
    afconvert_path = shutil.which("afconvert")
    if not say_path:
        raise RuntimeError("macOS 'say' is unavailable")
    if not afconvert_path:
        raise RuntimeError("macOS 'afconvert' is unavailable")

    rate_flags: list[str] = []
    if speed is not None:
        # macOS `say` default is ~175 words/min; scale it by the UI multiplier
        # (clamped to the same 0.7–1.2 band the cloud provider honors).
        wpm = int(round(175 * max(0.7, min(1.2, float(speed)))))
        rate_flags = ["-r", str(wpm)]

    with tempfile.TemporaryDirectory(prefix="pai-tts-") as tmp:
        aiff = Path(tmp) / "speech.aiff"
        m4a = Path(tmp) / "speech.m4a"
        _run(
            [say_path, *rate_flags, "-o", str(aiff), "-f", "-"],
            input_text=text,
            label="macOS 'say'",
            timeout=60,
        )
        _run(
            [afconvert_path, "-f", "m4af", "-d", "aac", str(aiff), str(m4a)],
            input_text=None,
            label="macOS 'afconvert'",
            timeout=30,
        )
        try:
            data = m4a.read_bytes()
        except FileNotFoundError as e:
            raise RuntimeError("macOS 'say' did not produce playable audio") from e

    if not data:
        raise RuntimeError("macOS 'say' produced empty playable audio")
    return data, "audio/mp4"


def _run(args: list[str], *, input_text: str | None, label: str, timeout: float) -> None:
    try:
        subprocess.run(
            args, input=input_text, text=True, capture_output=True, check=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{label} timed out") from e
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"{label} failed{f': {detail}' if detail else ''}") from e
