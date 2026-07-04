"""Whisper.cpp STT wrapper. Shells out to /usr/libexec/voice/whisper-cli."""

from __future__ import annotations

import subprocess
from pathlib import Path

from boot import paths

PAI_ROOT = paths.PAI_ROOT
LIBEXEC = PAI_ROOT / "usr" / "libexec" / "voice"
BINARY = LIBEXEC / "whisper-cli"
MODEL = LIBEXEC / "ggml-base.en.bin"


class STTError(RuntimeError):
    pass


def transcribe(wav_path: Path) -> str:
    """Run whisper.cpp on wav_path. Returns transcript text. Blocking."""
    if not BINARY.exists():
        raise STTError(f"whisper-cli not found at {BINARY}")
    if not MODEL.exists():
        raise STTError(f"model not found at {MODEL}")

    stem = wav_path.with_suffix("")
    txt_out = wav_path.with_suffix(".txt")
    # whisper-cli writes <of>.txt when -otxt and -of are passed.
    cmd = [
        str(BINARY),
        "-m", str(MODEL),
        "-f", str(wav_path),
        "-otxt",
        "-of", str(stem),
        "-nt",          # no timestamps in the output
        "-l", "en",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise STTError(f"whisper-cli failed (rc={proc.returncode}): {proc.stderr.strip()[:400]}")
    if not txt_out.exists():
        raise STTError(f"whisper-cli produced no transcript at {txt_out}")
    return txt_out.read_text().strip()
