"""Voice inbound driver.

Listens to the default mic, detects a wake word with openWakeWord, captures
the trailing utterance until VAD reports silence (or 15s cap), runs whisper.cpp
on the captured WAV, and emits a `voice:utterance` event.

Follow-up mode: after the PAI finishes talking (the console's read-aloud
playback ends), the web backend arms a short wake-free window by writing an
epoch deadline to /sys/drivers/voice/followup. While armed, sustained speech
onset opens a capture directly — the owner answers without repeating the wake
word. The file is one-shot (consumed on capture); each spoken reply re-arms it,
so a multi-turn voice conversation needs the wake word only once.

Concurrency: wake detection runs on the audio thread. STT runs in a worker
thread (via asyncio.to_thread) so a long transcription doesn't block the next
wake hit. Concurrent wake hits during transcription are queued.
"""

from __future__ import annotations

import asyncio
import re
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

from boot import paths
from boot import processes as P

# Heavy native deps (numpy, sounddevice, openwakeword/onnxruntime, whisper.cpp)
# are provisioned by libexec/install.sh, NOT the kernel's base venv. If a dep is
# missing, let the ImportError propagate: the proc goes `failed` and routes to
# root, which is the actual escalation path. Don't swallow it into a soft
# "unprovisioned" event — that hides a real error from root.
import numpy as np

from . import stt
from .wake import SAMPLE_RATE, WAKE_BLOCK, VAD_FRAME, WakeDetector, SilenceDetector, OnsetDetector

PAI_ROOT = paths.PAI_ROOT
CAPTURES_DIR = PAI_ROOT / "sys" / "drivers" / "voice" / "captures"
# Follow-up window control file: the web console arms it (writes an epoch
# deadline) when the PAI's read-aloud reply finishes playing, so the owner can
# answer without repeating the wake word. One-shot — consumed (unlinked) the
# moment speech onset opens a capture.
FOLLOWUP_FILE = PAI_ROOT / "sys" / "drivers" / "voice" / "followup"

WAKE_MODEL = "alexa"
WAKE_THRESHOLD = 0.7        # default 0.5 false-fires on ambient noise
WAKE_COOLDOWN_S = 1.5       # ignore new wake hits for this long after one fires
MIN_UTTERANCE_S = 0.5       # drop captures shorter than this (likely false trigger)
MIN_PEAK_RMS = 80           # int16 peak-RMS floor over a 250ms window
MAX_UTTERANCE_S = 15
SILENCE_TAIL_MS = 1000
FOLLOWUP_ONSET_MS = 150     # sustained speech needed to open a wake-free capture
FOLLOWUP_PREROLL_FRAMES = 6 # ~480ms of audio kept so onset detection doesn't clip the first word


def _followup_armed() -> bool:
    """True while the console-armed follow-up deadline is in the future."""
    try:
        return time.time() < float(FOLLOWUP_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False


def _disarm_followup() -> None:
    FOLLOWUP_FILE.unlink(missing_ok=True)


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


def _is_noise_transcript(text: str) -> bool:
    """True when whisper produced no real speech — empty, or only bracketed/
    parenthesized non-speech markers like "[BLANK_AUDIO]", "[silence]",
    "(wind blowing)". Those come from a false wake trigger on ambient noise;
    they must not emit an utterance, nudge the PAI, or leave an owner bubble."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    without_markers = re.sub(r"[\[(][^\])]*[\])]", "", stripped).strip()
    return not without_markers


def _owner_pai_slug() -> str | None:
    """The fallback (owner-facing) PAI — its transcript is the console's main
    conversation, where a spoken phrase belongs as a `me:` bubble. Read-only
    and tolerant; None if config is missing/unreadable."""
    try:
        from boot import config

        return next((name for name in config.load_config() if config.is_fallback(name)), None)
    except Exception:
        return None


def _echo_owner_bubble(text: str) -> None:
    """Mirror the heard phrase into the owner PAI's day-file as `[HH:MM] me:
    <text>` so the web console renders it as an owner message bubble (the hub
    watches the me-thread dir and rebroadcasts on change). Display-only:
    appending here does NOT nudge the kernel, so the `voice:utterance` event
    stays the sole trigger — the PAI responds once, not twice. Best-effort; a
    failure must never drop the utterance itself."""
    try:
        slug = _owner_pai_slug()
        if not slug:
            return
        day = paths.me_thread_today(slug)
        day.parent.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now().strftime('%H:%M')}] me: {text}\n"
        with day.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[voice-in] owner-bubble echo failed: {e}", flush=True)


async def _transcribe_and_emit(wav_path: Path, wake_word: str, duration_ms: int, captured_at: str) -> None:
    try:
        text = await asyncio.to_thread(stt.transcribe, wav_path)
    except stt.STTError as e:
        print(f"[voice-in] STT failed: {e}", flush=True)
        P.emit_event({"source": "voice", "kind": "wake_failed", "reason": f"stt: {e}"})
        return
    text = (text or "").strip()
    if _is_noise_transcript(text):
        print(f"[voice-in] non-speech transcript ({text!r}); dropping", flush=True)
        return
    # Echo as an owner bubble BEFORE emitting: the PAI's nudge (fired by the
    # event below) reads its transcript for context, so the `me:` line should
    # already be there when it wakes.
    _echo_owner_bubble(text)
    P.emit_event({
        "source": "voice",
        "kind": "utterance",
        "text": text,
        "audio_path": str(wav_path.relative_to(PAI_ROOT)),
        "wake_word": wake_word,
        "duration_ms": duration_ms,
        "captured_at": captured_at,
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
    trigger = WAKE_MODEL  # what opened the live capture: wake model name or "followup"
    silence = SilenceDetector(aggressiveness=2, silence_ms=SILENCE_TAIL_MS)
    onset = OnsetDetector(aggressiveness=2, speech_ms=FOLLOWUP_ONSET_MS)
    followup_preroll: deque[np.ndarray] = deque(maxlen=FOLLOWUP_PREROLL_FRAMES)

    with stream:
        print(f"[voice-in] listening on default input device, "
              f"wake_word={WAKE_MODEL} threshold={WAKE_THRESHOLD}", flush=True)
        while True:
            frame = await queue.get()
            now = loop.time()

            if not capturing:
                if now < cooldown_until:
                    continue

                # Follow-up window: after the PAI finishes talking, the console
                # arms FOLLOWUP_FILE so the very next utterance opens a capture
                # on speech onset alone — no wake word. The wake word still
                # works during the window (checked below if onset hasn't fired).
                if _followup_armed():
                    followup_preroll.append(frame)
                    buf = frame.tobytes()
                    onset_hit = False
                    for off in range(0, len(buf) - VAD_FRAME * 2 + 1, VAD_FRAME * 2):
                        if onset.feed(buf[off:off + VAD_FRAME * 2]):
                            onset_hit = True
                            break
                    if onset_hit:
                        _disarm_followup()
                        print("[voice-in] follow-up speech onset — capturing (no wake word)", flush=True)
                        capturing = True
                        captured = list(followup_preroll)
                        followup_preroll.clear()
                        onset.reset()
                        trigger = "followup"
                        capture_started_at = now
                        capture_iso = datetime.now().isoformat(timespec="seconds")
                        P.emit_event({
                            "source": "voice",
                            "kind": "listening",
                            "wake_word": trigger,
                            "captured_at": capture_iso,
                        })
                        silence.reset()
                        continue
                elif followup_preroll:
                    followup_preroll.clear()
                    onset.reset()

                hit = await asyncio.to_thread(wake.feed, frame)
                if hit is None:
                    continue
                print(f"[voice-in] wake hit: {hit.model} score={hit.score:.2f}", flush=True)
                capturing = True
                captured = [frame]
                trigger = hit.model
                capture_started_at = now
                capture_iso = datetime.now().isoformat(timespec="seconds")
                # Fire the instant the wake word lands, before any capture/STT —
                # the web surface lights up "Speaking: …" off this. Carries no
                # text (none transcribed yet); `voice:utterance` follows later.
                P.emit_event({
                    "source": "voice",
                    "kind": "listening",
                    "wake_word": hit.model,
                    "captured_at": capture_iso,
                })
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
                onset.reset()
                if elapsed < MIN_UTTERANCE_S or rms < MIN_PEAK_RMS:
                    print(f"[voice-in] dropping false trigger "
                          f"(elapsed={elapsed:.2f}s peak_rms={rms:.0f})", flush=True)
                else:
                    wav_path = CAPTURES_DIR / f"{capture_iso.replace(':', '')}.wav"
                    _write_wav(wav_path, captured)
                    duration_ms = int(elapsed * 1000)
                    asyncio.create_task(
                        _transcribe_and_emit(wav_path, trigger, duration_ms, capture_iso)
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
