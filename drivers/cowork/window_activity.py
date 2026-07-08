"""Cowork window/clipboard tracker — in-kernel supervisor.

The actual OS observation lives in `window_sidecar.py`, a small subprocess,
because NSWorkspace activation notifications only deliver on a main-thread
runloop and the kernel's main thread belongs to asyncio (probed 2026-07-07).
This process supervises the sidecar, turns its JSON lines into ndjson log
entries and kernel events, and enforces the cowork capabilities live. The
sidecar serves two facets — window focus and clipboard sampling — each gated
by its own freeze file:

- both window.freeze and clipboard.freeze present → sidecar is not running
  at all; nothing is captured, not even discarded.
- one facet frozen → sidecar keeps running for the other; the frozen kind
  is dropped at _handle_line.
- a freeze removed → sidecar restarts on the watchdog event, no kernel
  restart.
"""
import asyncio
import json
from pathlib import Path

from boot import paths
from boot import processes as P
from drivers.cowork import common

WINDOW_LOG = common.STATE_DIR / "window_activity.ndjson"
CLIPBOARD_LOG = common.STATE_DIR / "clipboard.ndjson"
SIDECAR = Path(__file__).resolve().parent / "window_sidecar.py"
RESPAWN_BACKOFF = 5.0


def _start_freeze_watch(loop, q: asyncio.Queue):
    """Watchdog observer on the driver state dir: any event touching a
    freeze file wakes the supervisor loop. Event-driven, never polled."""
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            paths_ = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
            if any(str(p).endswith(".freeze") for p in paths_ if p):
                try:
                    loop.call_soon_threadsafe(q.put_nowait, "freeze-flip")
                except RuntimeError:
                    pass

    observer = Observer()
    observer.schedule(_Handler(), str(common.STATE_DIR), recursive=False)
    observer.daemon = True
    observer.start()
    return observer


async def _spawn_sidecar() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        str(paths.venv_python()), str(SIDECAR),
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # inherit → kernel log
        cwd=str(paths.PAI_ROOT),
    )


def _handle_line(raw: bytes) -> str | None:
    """Process one sidecar line. Returns 'fatal' to stop the driver."""
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    kind = obj.pop("type", None)
    if kind == "ready":
        print("[cowork-window] sidecar ready — watching app activations", flush=True)
        return None
    if kind == "fatal":
        reason = obj.get("reason", "?")
        if reason == "ax_untrusted":
            print(
                "[cowork-window] Accessibility not granted for this process — "
                "grant it in System Settings > Privacy & Security > "
                "Accessibility; exiting cleanly",
                flush=True,
            )
        else:
            print(f"[cowork-window] sidecar fatal: {reason}; exiting", flush=True)
        return "fatal"
    # Per-facet gate, checked per line: one facet can be frozen while the
    # sidecar keeps running for the other.
    if kind == "window":
        if not common.capture_enabled("window"):
            return None
        common.append_ndjson(WINDOW_LOG, obj)
        P.emit_event({"source": "cowork", "kind": "window_changed", **obj})
    elif kind == "clipboard":
        if not common.capture_enabled("clipboard"):
            return None
        content = obj.get("content")
        if isinstance(content, str) and len(content) > common.NDJSON_TEXT_CAP:
            content = content[: common.NDJSON_TEXT_CAP]
        entry = {
            "ts": obj.get("ts"),
            "app": obj.get("app"),
            "content": content,
            "type": obj.get("clip_type"),
        }
        common.append_ndjson(CLIPBOARD_LOG, entry)
        ev_text, truncated = common.event_text(content)
        P.emit_event({
            "source": "cowork", "kind": "clipboard_changed",
            "app": entry["app"], "content": ev_text, "truncated": truncated,
            "type": entry["type"], "ts": entry["ts"],
        })
    return None


def _sidecar_needed() -> bool:
    """The sidecar serves the window and clipboard facets; it only stops
    when both are frozen."""
    return common.capture_enabled("window") or common.capture_enabled("clipboard")


async def run() -> None:
    common.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not SIDECAR.exists():
        print(f"[cowork-window] sidecar missing at {SIDECAR}; driver idle", flush=True)
        return
    loop = asyncio.get_running_loop()
    ctl: asyncio.Queue = asyncio.Queue()
    observer = _start_freeze_watch(loop, ctl)
    proc: asyncio.subprocess.Process | None = None
    try:
        while True:
            if not _sidecar_needed():
                print(
                    "[cowork-window] window + clipboard capture off — paused",
                    flush=True,
                )
                while not _sidecar_needed():
                    await ctl.get()
                print("[cowork-window] capture re-enabled — resuming", flush=True)
            proc = await _spawn_sidecar()
            read_task = asyncio.ensure_future(proc.stdout.readline())
            ctl_task = asyncio.ensure_future(ctl.get())
            fatal = False
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {read_task, ctl_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if ctl_task in done:
                        ctl_task = asyncio.ensure_future(ctl.get())
                        if not _sidecar_needed():
                            break  # both facets frozen → stop the sidecar entirely
                    if read_task in done:
                        line = read_task.result()
                        if not line:
                            break  # sidecar exited
                        if _handle_line(line) == "fatal":
                            fatal = True
                            break
                        read_task = asyncio.ensure_future(proc.stdout.readline())
            finally:
                for t in (read_task, ctl_task):
                    t.cancel()
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), 5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            if fatal:
                return
            if _sidecar_needed():
                # sidecar died on its own — back off once, then respawn
                print(
                    f"[cowork-window] sidecar exited rc={proc.returncode}; "
                    f"respawning in {RESPAWN_BACKOFF}s",
                    flush=True,
                )
                await asyncio.sleep(RESPAWN_BACKOFF)
    except asyncio.CancelledError:
        print("[cowork-window] stopped", flush=True)
        raise
    finally:
        observer.stop()
        if proc is not None and proc.returncode is None:
            proc.kill()
