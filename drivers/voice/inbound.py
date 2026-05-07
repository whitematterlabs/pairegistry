"""Voice inbound driver.

Listens to the default mic, detects a wake word with openWakeWord, captures
the trailing utterance until VAD reports silence (or 15s cap), runs whisper.cpp
on the captured WAV, and emits a `voice:utterance` event.

Concurrency: wake detection runs on the audio thread. STT runs in a worker
thread (via asyncio.to_thread) so a long transcription doesn't block the next
wake hit. Concurrent wake hits during transcription are queued.
"""

from __future__ import annotations

import asyncio
import os
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from boot import processes as P

from . import stt
from .wake import SAMPLE_RATE, WAKE_BLOCK, VAD_FRAME, WakeDetector, SilenceDetector

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
CAPTURES_DIR = PAI_ROOT / "sys" / "drivers" / "voice" / "captures"

WAKE_MODEL = "hey_jarvis"
WAKE_THRESHOLD = 0.7        # default 0.5 false-fires on ambient noise
WAKE_COOLDOWN_S = 1.5       # ignore new wake hits for this long after one fires
MIN_UTTERANCE_S = 0.5       # drop captures shorter than this (likely false trigger)
MIN_PEAK_RMS = 80           # int16 peak-RMS floor over a 250ms window
MAX_UTTERANCE_S = 15
SILENCE_TAIL_MS = 1000


def _peak_rms(frames: list[np.ndarray], window_ms: int = 250) -> float:
    """RMS of the loudest `window_ms` slice — robust to silence tails."""
    if not frames:
        return 0.0
    audio = np.concatenate(frames).astype(np.float32)
    win = SAMPLE_RATE * window_ms // 1000
    if len(audio) <= win:
        return float(np.sqrt(np.mean(audio * audio)))
    # Sliding-window RMS via cumulative sum of squares.
    sq = audio * audio
    cs = np.cumsum(sq)
    win_sums = cs[win:] - cs[:-win]
    return float(np.sqrt(win_sums.max() / win))


def _write_wav(path: Path, frames: list[np.ndarray]) -> None:
    """Concatenate int16 frames and write a 16kHz mono WAV."""
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())


async def _transcribe_and_emit(wav_path: Path, wake_word: str, duration_ms: int, captured_at: str) -> None:
    try:
        text = await asyncio.to_thread(stt.transcribe, wav_path)
    except stt.STTError as e:
        print(f"[voice-in] STT failed: {e}", flush=True)
        P.emit_event({"source": "voice", "kind": "wake_failed", "reason": f"stt: {e}"})
        return
    if not text:
        print("[voice-in] empty transcript; dropping", flush=True)
        return
    P.emit_event({
        "source": "voice",
        "kind": "utterance",
        "text": text,
        "audio_path": str(wav_path.relative_to(PAI_ROOT)),
        "wake_word": wake_word,
        "duration_ms": duration_ms,
        "captured_at": captured_at,
        "reply_with": (
            "The owner spoke to you. Reply by voice: run `say \"your reply\"` "
            "in your shell. Plain prose, no markdown, short sentences. "
            "The transcript may have STT errors — interpret charitably."
        ),
    })
    print(f"[voice-in] emitted utterance ({duration_ms}ms): {text[:80]}", flush=True)


async def _audio_loop() -> None:
    """Pull mic frames, run wake detection, capture utterances, dispatch STT."""
    import sounddevice as sd

    wake = WakeDetector([WAKE_MODEL], threshold=WAKE_THRESHOLD)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)

    def _callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            print(f"[voice-in] sd status: {status}", flush=True)
        # indata is shape (WAKE_BLOCK, 1) int16 — flatten to 1D
        chunk = np.frombuffer(bytes(indata), dtype=np.int16).copy()
        try:
            loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except asyncio.QueueFull:
            pass  # drop if backlogged

    stream = sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=WAKE_BLOCK,
        dtype="int16",
        channels=1,
        callback=_callback,
    )

    capturing = False
    captured: list[np.ndarray] = []
    capture_started_at: float | None = None
    capture_iso: str = ""
    cooldown_until: float = 0.0
    silence = SilenceDetector(aggressiveness=2, silence_ms=SILENCE_TAIL_MS)

    with stream:
        print(f"[voice-in] listening on default input device, "
              f"wake_word={WAKE_MODEL} threshold={WAKE_THRESHOLD}", flush=True)
        while True:
            frame = await queue.get()
            now = loop.time()

            if not capturing:
                if now < cooldown_until:
                    continue
                hit = wake.feed(frame)
                if hit is None:
                    continue
                print(f"[voice-in] wake hit: {hit.model} score={hit.score:.2f}", flush=True)
                capturing = True
                captured = [frame]
                capture_started_at = now
                capture_iso = datetime.now().isoformat(timespec="seconds")
                silence.reset()
                continue

            # capturing
            captured.append(frame)
            elapsed = now - (capture_started_at or now)

            # Slice the WAKE_BLOCK frame into VAD_FRAME-sized chunks for the VAD.
            done = False
            buf = frame.tobytes()
            for off in range(0, len(buf) - VAD_FRAME * 2 + 1, VAD_FRAME * 2):
                if silence.is_done(buf[off:off + VAD_FRAME * 2]):
                    done = True
                    break

            if done or elapsed >= MAX_UTTERANCE_S:
                if elapsed >= MAX_UTTERANCE_S:
                    print(f"[voice-in] max utterance cap hit ({MAX_UTTERANCE_S}s)", flush=True)

                rms = _peak_rms(captured)
                cooldown_until = now + WAKE_COOLDOWN_S
                # Reset the wake model so its sliding-window buffer doesn't
                # carry stale audio from the just-finished utterance and
                # immediately re-fire on the next frame after cooldown.
                try:
                    wake.model.reset()
                except Exception:
                    pass
                if elapsed < MIN_UTTERANCE_S or rms < MIN_PEAK_RMS:
                    print(f"[voice-in] dropping false trigger "
                          f"(elapsed={elapsed:.2f}s peak_rms={rms:.0f})", flush=True)
                else:
                    wav_path = CAPTURES_DIR / f"{capture_iso.replace(':', '')}.wav"
                    _write_wav(wav_path, captured)
                    duration_ms = int(elapsed * 1000)
                    asyncio.create_task(
                        _transcribe_and_emit(wav_path, WAKE_MODEL, duration_ms, capture_iso)
                    )

                capturing = False
                captured = []
                capture_started_at = None


async def run() -> None:
    print("[voice-in] starting", flush=True)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await _audio_loop()
    except asyncio.CancelledError:
        print("[voice-in] stopped", flush=True)
        raise
    except Exception as e:
        print(f"[voice-in] fatal: {e!r}", flush=True)
        P.emit_event({"source": "voice", "kind": "wake_failed", "reason": repr(e)})
        raise
