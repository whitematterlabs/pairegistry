"""Voice outbound driver.

Watches every PAI instance's `outbox/voice/` for new `.txt` files. Each new
file is spoken with `say` (macOS TTS), then moved to `.sent/` for audit.
On `say` failure, emits a `voice:say_failed` event and leaves the file in place.

Swap to ElevenLabs later by replacing _speak() — no event-shape change.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from boot import processes as P
from boot import paths

PAI_ROOT = Path(os.environ.get("PAI_ROOT", str(Path.home() / ".pai")))
INSTANCES_ROOT = PAI_ROOT / "var" / "lib" / "instances"

SAY_VOICE = "Samantha"
SAY_RATE = "200"


def _outbox_dirs() -> list[Path]:
    """All currently-existing /var/lib/instances/<pai>/outbox/voice/ dirs."""
    out: list[Path] = []
    if not INSTANCES_ROOT.is_dir():
        return out
    for inst in INSTANCES_ROOT.iterdir():
        if not inst.is_dir():
            continue
        d = inst / "outbox" / "voice"
        if d.is_dir():
            out.append(d)
    return out


def _ensure_outbox(inst: Path) -> Path:
    d = inst / "outbox" / "voice"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".sent").mkdir(exist_ok=True)
    return d


class _Handler(FileSystemEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.loop = loop
        self.queue = queue

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if p.suffix != ".txt":
            return
        if p.parent.name == ".sent":
            return
        self.loop.call_soon_threadsafe(self.queue.put_nowait, p)


async def _speak(text: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "say", "-r", SAY_RATE, "-v", SAY_VOICE, text,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return proc.returncode or 0, (stderr or b"").decode(errors="replace")


async def _drain(queue: asyncio.Queue) -> None:
    while True:
        path: Path = await queue.get()
        if not path.exists():
            continue
        try:
            text = path.read_text().strip()
        except Exception as e:
            print(f"[voice-out] read error {path}: {e!r}", flush=True)
            continue
        if not text:
            path.unlink(missing_ok=True)
            continue
        print(f"[voice-out] speaking ({len(text)}c) from {path.name}", flush=True)
        rc, err = await _speak(text)
        if rc == 0:
            sent_dir = path.parent / ".sent"
            sent_dir.mkdir(exist_ok=True)
            try:
                path.rename(sent_dir / path.name)
            except Exception as e:
                print(f"[voice-out] move-to-sent failed: {e!r}", flush=True)
        else:
            reason = f"say rc={rc}: {err.strip()[:200]}"
            print(f"[voice-out] {reason}", flush=True)
            P.emit_event({
                "source": "voice",
                "kind": "say_failed",
                "path": str(path.relative_to(PAI_ROOT)),
                "reason": reason,
            })


async def _rescan_loop(observer: Observer, handler: _Handler, watched: set[Path]) -> None:
    """Periodically discover new instances/outboxes and watch them."""
    while True:
        for inst in INSTANCES_ROOT.iterdir() if INSTANCES_ROOT.is_dir() else []:
            if not inst.is_dir():
                continue
            d = _ensure_outbox(inst)
            if d not in watched:
                observer.schedule(handler, str(d), recursive=False)
                watched.add(d)
                print(f"[voice-out] watching {d}", flush=True)
                # Pick up any pre-existing files
                for f in sorted(d.glob("*.txt")):
                    handler.queue.put_nowait(f)
        await asyncio.sleep(10)


async def run() -> None:
    print("[voice-out] starting", flush=True)
    INSTANCES_ROOT.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    handler = _Handler(loop, queue)
    observer = Observer()
    watched: set[Path] = set()

    observer.start()
    drain_task = asyncio.create_task(_drain(queue))
    rescan_task = asyncio.create_task(_rescan_loop(observer, handler, watched))

    try:
        await asyncio.gather(drain_task, rescan_task)
    except asyncio.CancelledError:
        raise
    finally:
        drain_task.cancel()
        rescan_task.cancel()
        for t in (drain_task, rescan_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        observer.stop()
        observer.join(timeout=2)
        print("[voice-out] stopped", flush=True)
