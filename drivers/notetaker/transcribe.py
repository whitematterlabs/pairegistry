"""Notetaker transcription: finalize raw PCM into one 16k mono WAV, then
local whisper.cpp (default) or cloud STT (per-session opt-in).

Local path reuses the voice driver's provisioned whisper-cli/model but asks
for JSON output (-oj) to get segment timestamps; falls back to the plain
stt.transcribe() text as a single segment. Cloud path compresses to m4a and
sends through drivers.voice_cloud.provider (chunked if >24MB — the API caps
uploads at 25MB); cloud gives no timestamps, so chunk boundaries become the
segment boundaries.

Blocking — callers run it in a thread.
"""
import json
import subprocess
from pathlib import Path

import yaml

CLOUD_CHUNK_SECONDS = 600
CLOUD_MAX_BYTES = 24 * 1024 * 1024


class TranscribeError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TranscribeError(f"{what} failed (rc={proc.returncode}): {proc.stderr.strip()[:400]}")


def finalize_wav(session_dir: Path) -> Path:
    """Mix whatever raw streams exist (mic.raw int16 mono, system.raw float32)
    into audio16.wav (16k mono). Raises if neither stream has data."""
    meta = yaml.safe_load((session_dir / "meta.yaml").read_text()) or {}
    mic = session_dir / "mic.raw"
    system = session_dir / "system.raw"
    out = session_dir / "audio16.wav"
    inputs: list[list[str]] = []
    if system.exists() and system.stat().st_size > 0:
        s = meta.get("system", {})
        inputs.append([
            "-f", "f32le",
            "-ar", str(int(s.get("rate", 48000))),
            "-ac", str(int(s.get("channels", 2))),
            "-i", str(system),
        ])
    if mic.exists() and mic.stat().st_size > 0:
        m = meta.get("mic", {})
        inputs.append([
            "-f", "s16le",
            "-ar", str(int(m.get("rate", 16000))),
            "-ac", str(int(m.get("channels", 1))),
            "-i", str(mic),
        ])
    if not inputs:
        raise TranscribeError("no audio captured (both streams empty)")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for i in inputs:
        cmd += i
    if len(inputs) == 2:
        cmd += ["-filter_complex", "amix=inputs=2:duration=longest:normalize=1"]
    cmd += ["-ar", "16000", "-ac", "1", str(out)]
    _run(cmd, "ffmpeg mix")
    return out


def transcribe_local(wav: Path) -> list[dict]:
    """whisper.cpp with JSON output → [{start, end, text}] (seconds)."""
    from drivers.voice import stt

    if not stt.BINARY.exists() or not stt.MODEL.exists():
        raise TranscribeError(
            f"whisper-cli/model missing under {stt.LIBEXEC} — is the voice "
            "driver installed?"
        )
    stem = wav.with_suffix("")
    json_out = wav.with_suffix(".json")
    cmd = [
        str(stt.BINARY), "-m", str(stt.MODEL), "-f", str(wav),
        "-oj", "-of", str(stem), "-l", "en",
    ]
    _run(cmd, "whisper-cli")
    try:
        data = json.loads(json_out.read_text())
        segments = [
            {
                "start": round(t["offsets"]["from"] / 1000.0, 2),
                "end": round(t["offsets"]["to"] / 1000.0, 2),
                "text": t["text"].strip(),
            }
            for t in data["transcription"]
            if t.get("text", "").strip()
        ]
        if segments:
            return segments
    except (ValueError, KeyError, OSError):
        pass
    # -oj parse failed → plain-text fallback, one segment
    text = stt.transcribe(wav)
    return [{"start": 0.0, "end": 0.0, "text": text}] if text else []


def transcribe_cloud(wav: Path) -> list[dict]:
    """Cloud STT via the voice_cloud provider; chunked when the compressed
    file would blow the upload cap. Chunk boundaries become segments."""
    from drivers.voice_cloud import provider as cloud

    m4a = wav.with_suffix(".m4a")
    _run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(wav), "-c:a", "aac", "-b:a", "64k", str(m4a)],
        "ffmpeg m4a",
    )
    if m4a.stat().st_size <= CLOUD_MAX_BYTES:
        text = cloud.transcribe(
            m4a.read_bytes(), content_type="audio/mp4", filename=m4a.name
        )
        m4a.unlink(missing_ok=True)
        return [{"start": 0.0, "end": 0.0, "text": text.strip()}] if text.strip() else []
    # chunk into CLOUD_CHUNK_SECONDS pieces
    chunk_dir = wav.parent / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    _run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(m4a), "-f", "segment",
         "-segment_time", str(CLOUD_CHUNK_SECONDS),
         "-c", "copy", str(chunk_dir / "chunk%04d.m4a")],
        "ffmpeg segment",
    )
    m4a.unlink(missing_ok=True)
    segments: list[dict] = []
    for i, chunk in enumerate(sorted(chunk_dir.glob("chunk*.m4a"))):
        text = cloud.transcribe(
            chunk.read_bytes(), content_type="audio/mp4", filename=chunk.name
        )
        if text.strip():
            segments.append({
                "start": float(i * CLOUD_CHUNK_SECONDS),
                "end": float((i + 1) * CLOUD_CHUNK_SECONDS),
                "text": text.strip(),
            })
        chunk.unlink(missing_ok=True)
    chunk_dir.rmdir()
    return segments


def transcribe_session(session_dir: Path, cloud: bool) -> dict:
    """Finalize + transcribe one session. Returns the transcript dict (also
    written to transcript.json). On success deletes the audio (privacy
    default: other people's raw audio is not retained); on failure the raw
    streams are kept for retry and the exception propagates."""
    meta = yaml.safe_load((session_dir / "meta.yaml").read_text()) or {}
    wav = finalize_wav(session_dir)
    try:
        segments = transcribe_cloud(wav) if cloud else transcribe_local(wav)
    except Exception:
        wav.unlink(missing_ok=True)  # keep the raw streams, drop the derivative
        raise
    transcript = {
        "session_id": session_dir.name,
        "started": meta.get("started"),
        "ended": meta.get("ended"),
        "cloud": bool(cloud),
        "mic_captured": bool(meta.get("mic", {}).get("captured", False)),
        "segments": segments,
    }
    (session_dir / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=1) + "\n"
    )
    for name in ("mic.raw", "system.raw", "audio16.wav", "audio16.json", "audio16.txt"):
        (session_dir / name).unlink(missing_ok=True)
    return transcript
