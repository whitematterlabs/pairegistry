"""Notetaker recorder — owner-triggered call recording + transcription.

Trigger surface: YAML command files dropped into
sys/drivers/notetaker/commands/ ({action: start|stop, cloud: bool}) watched
with a watchdog Observer (the email-drafts spool pattern) — the kernel event
bus routes to PAIs, not to drivers, so owner actions arrive as files. Files
are consumed (deleted) after processing.

Two-tier consent: the `notetaker` capability (default no; the kernel
projects it into capture.freeze here) must be enabled once, and every
recording needs an explicit per-session start command. Never records
ambiently; never silently — a `recording` marker file drives the console
indicator, and recording_started/stopped events let the PAI announce it.

Raw audio is deleted after successful transcription; kept for retry on
failure. A session interrupted by a crash/shutdown is finalized and
transcribed on the next driver start.
"""
import asyncio
from pathlib import Path

import yaml

from boot import paths
from boot import processes as P
from drivers.notetaker import capture, transcribe

STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "notetaker"
FREEZE_PATH = STATE_DIR / "capture.freeze"
COMMANDS_DIR = STATE_DIR / "commands"
SESSIONS_DIR = STATE_DIR / "sessions"
MARKER = STATE_DIR / "recording"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(kind: str, **payload) -> None:
    P.emit_event({"source": "notetaker", "kind": kind, **payload})


class Session:
    def __init__(self):
        self.session_id = _now_iso().replace(":", "").replace("-", "").lower()
        self.dir = SESSIONS_DIR / self.session_id
        self.cloud = False
        self.tap: capture.TapCapture | None = None
        self.mic: capture.MicCapture | None = None

    def start(self, cloud: bool) -> None:
        self.cloud = cloud
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "status").write_text("recording\n")
        self.tap = capture.TapCapture(self.dir / "system.raw", self.session_id)
        self.tap.start()  # raises TapPermissionError → caller surfaces it
        mic_ok = True
        self.mic = capture.MicCapture(self.dir / "mic.raw")
        try:
            self.mic.start()
        except Exception as e:
            mic_ok = False
            self.mic = None
            print(f"[notetaker] mic unavailable ({e!r}) — recording system audio only", flush=True)
        meta = {
            "session_id": self.session_id,
            "started": _now_iso(),
            "cloud": cloud,
            "system": {
                "rate": self.tap.sample_rate,
                "channels": self.tap.channels,
                "format": "f32le",
            },
            "mic": {
                "captured": mic_ok,
                "rate": self.mic.sample_rate if self.mic else 0,
                "channels": 1,
                "format": "s16le",
            },
        }
        (self.dir / "meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
        MARKER.write_text(self.session_id + "\n")
        _emit("recording_started", session_id=self.session_id, cloud=cloud,
              mic_captured=mic_ok, ts=_now_iso())
        print(f"[notetaker] recording session {self.session_id} (cloud={cloud}, mic={mic_ok})", flush=True)

    def stop_capture(self) -> None:
        if self.tap is not None:
            self.tap.stop()
            self.tap = None
        if self.mic is not None:
            self.mic.stop()
            self.mic = None
        # patch ended into meta
        meta_path = self.dir / "meta.yaml"
        try:
            meta = yaml.safe_load(meta_path.read_text()) or {}
            meta["ended"] = _now_iso()
            meta_path.write_text(yaml.safe_dump(meta, sort_keys=False))
        except OSError:
            pass
        MARKER.unlink(missing_ok=True)


async def _transcribe_and_emit(session_dir: Path, cloud: bool) -> None:
    sid = session_dir.name
    try:
        await asyncio.to_thread(transcribe.transcribe_session, session_dir, cloud)
    except Exception as e:
        (session_dir / "status").write_text("failed\n")
        print(f"[notetaker] transcription failed for {sid}: {e!r} (audio kept)", flush=True)
        _emit("transcript_failed", session_id=sid, error=str(e)[:500], ts=_now_iso())
        return
    (session_dir / "status").write_text("done\n")
    rel = (session_dir / "transcript.json").relative_to(paths.PAI_ROOT)
    print(f"[notetaker] transcript ready: {rel}", flush=True)
    _emit("transcript_ready", session_id=sid, transcript_path=str(rel),
          cloud=cloud, ts=_now_iso())


def _recover_interrupted() -> list[tuple[Path, bool]]:
    """Sessions left in `recording` state by a crash/shutdown: finalize what
    was written and queue them for transcription rather than discarding."""
    out: list[tuple[Path, bool]] = []
    if not SESSIONS_DIR.is_dir():
        return out
    for d in sorted(SESSIONS_DIR.iterdir()):
        status = d / "status"
        try:
            if status.read_text().strip() != "recording":
                continue
        except OSError:
            continue
        status.write_text("interrupted\n")
        try:
            meta = yaml.safe_load((d / "meta.yaml").read_text()) or {}
        except OSError:
            continue
        print(f"[notetaker] recovering interrupted session {d.name}", flush=True)
        out.append((d, bool(meta.get("cloud", False))))
    return out


def _start_command_watch(loop, q: asyncio.Queue):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                try:
                    loop.call_soon_threadsafe(q.put_nowait, Path(event.src_path))
                except RuntimeError:
                    pass

        def on_moved(self, event):
            if not event.is_directory:
                try:
                    loop.call_soon_threadsafe(q.put_nowait, Path(event.dest_path))
                except RuntimeError:
                    pass

    observer = Observer()
    observer.schedule(_Handler(), str(COMMANDS_DIR), recursive=False)
    observer.daemon = True
    observer.start()
    return observer


def _read_command(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        data = None
    path.unlink(missing_ok=True)  # consume regardless — commands are one-shot
    return data if isinstance(data, dict) else None


async def run() -> None:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    MARKER.unlink(missing_ok=True)  # stale marker from a crash
    for session_dir, cloud in _recover_interrupted():
        asyncio.ensure_future(_transcribe_and_emit(session_dir, cloud))
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    observer = _start_command_watch(loop, q)
    for p in sorted(COMMANDS_DIR.glob("*.yaml")):  # commands from while we were down
        q.put_nowait(p)
    current: Session | None = None
    print("[notetaker] ready — watching for start/stop commands", flush=True)
    try:
        while True:
            path = await q.get()
            cmd = _read_command(path)
            if cmd is None:
                continue
            action = str(cmd.get("action", "")).strip().lower()
            if action == "start":
                if FREEZE_PATH.exists():
                    print("[notetaker] start refused: capability disabled", flush=True)
                    _emit("start_failed", reason="capability_disabled", ts=_now_iso(),
                          detail="the notetaker capability is off — enable it in the console first")
                    continue
                if current is not None:
                    print("[notetaker] start ignored: already recording", flush=True)
                    _emit("start_failed", reason="already_recording",
                          session_id=current.session_id, ts=_now_iso())
                    continue
                session = Session()
                try:
                    await asyncio.to_thread(session.start, bool(cmd.get("cloud", False)))
                except capture.TapPermissionError as e:
                    session.stop_capture()
                    (session.dir / "status").write_text("failed\n")
                    print(f"[notetaker] start refused: {e}", flush=True)
                    _emit("start_failed", reason="permission", detail=str(e), ts=_now_iso())
                    continue
                except Exception as e:
                    session.stop_capture()
                    (session.dir / "status").write_text("failed\n")
                    print(f"[notetaker] start failed: {e!r}", flush=True)
                    _emit("start_failed", reason="error", detail=repr(e)[:500], ts=_now_iso())
                    continue
                current = session
            elif action == "stop":
                if current is None:
                    print("[notetaker] stop ignored: not recording", flush=True)
                    continue
                session, current = current, None
                await asyncio.to_thread(session.stop_capture)
                (session.dir / "status").write_text("transcribing\n")
                _emit("recording_stopped", session_id=session.session_id, ts=_now_iso())
                print(f"[notetaker] stopped {session.session_id}; transcribing", flush=True)
                asyncio.ensure_future(
                    _transcribe_and_emit(session.dir, session.cloud)
                )
            else:
                print(f"[notetaker] unknown command action {action!r} ignored", flush=True)
    except asyncio.CancelledError:
        print("[notetaker] stopped", flush=True)
        raise
    finally:
        observer.stop()
        if current is not None:
            # finalize on shutdown; transcription happens via recovery next boot
            current.stop_capture()
            (current.dir / "status").write_text("recording\n")  # let recovery pick it up
