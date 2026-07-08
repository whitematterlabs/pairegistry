"""Cowork file-activity tracker: one FSEventStream over the owner's real
home, per-file events, two-layer noise suppression (FSEvents exclusion paths
for the big prefix offenders; an in-callback denylist for the scattered
patterns). Logs raw change flags — never a classified operation — and
enriches created/renamed paths with the kMDItemWhereFroms source URL.

Requires Full Disk Access for complete coverage; without it macOS silently
redacts protected subtrees (we log one warning and keep going — FDA granted
later is picked up on the next event with no code change).

Probed 2026-07-07: FSEvents delivers fine on a dedicated background-thread
runloop (unlike NSWorkspace), so this runs in-kernel with no sidecar.
"""
import asyncio
import os
import plistlib
import pwd
import threading
import time
from pathlib import Path

from boot import paths
from boot import processes as P
from drivers.cowork import common

FILE_LOG = common.STATE_DIR / "file_activity.ndjson"
RUNLOOP_SLICE = 1.0
FSEVENTS_LATENCY = 1.0  # kernel-side batching; not a poll


def _exclusions(home: Path) -> list[str]:
    """Layer 1 — FSEvents exclusion paths (kernel-side, prefix-matched, max
    8). PAI_ROOT is excluded above all: the runtime's own churn (logs,
    events, these very ndjson appends) must never feed back into the
    stream."""
    return [str(p) for p in (
        home / "Library",
        paths.PAI_ROOT,
        home / ".cache",
        home / ".Trash",
        home / ".npm",
        home / ".cargo",
        home / ".local",
        home / ".claude",
    )]


# Layer 2 — in-callback denylist for what prefixes can't catch.
_DENY_SEGMENTS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", "Caches",
    "DerivedData", ".Trash", ".npm", ".cargo", ".rustup", ".gradle", ".m2",
    ".docker", ".ollama", ".nvm", ".pyenv", ".vscode", ".cursor", ".pai",
}
_DENY_SUFFIXES = (
    ".tmp", ".part", ".swp", ".swx", "-wal", "-shm", "-journal",
    ".crdownload", ".download",
)
_DENY_BASENAMES = {".DS_Store", ".localized"}

_FLAG_NAMES: list[tuple[int, str]] = []  # populated in run() after import


def _denied(path: str) -> bool:
    p = Path(path)
    if p.name in _DENY_BASENAMES or p.name.endswith(_DENY_SUFFIXES):
        return True
    return bool(_DENY_SEGMENTS.intersection(p.parts))


def _change_names(flags: int) -> list[str]:
    return [name for bit, name in _FLAG_NAMES if flags & bit]


def _source_url(path: str) -> str | None:
    try:
        raw = os.getxattr(path, "com.apple.metadata:kMDItemWhereFroms")
        vals = plistlib.loads(raw)
        if isinstance(vals, list) and vals:
            return str(vals[0])
    except OSError:
        pass
    except Exception:
        pass
    return None


class _StreamWatcher:
    """Dedicated thread owning the FSEventStream + its runloop."""

    def __init__(self, root: Path, on_batch):
        self._root = root
        self._on_batch = on_batch  # callable(list[tuple[path, flags]])
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="cowork-files-runloop", daemon=True
        )
        self.failed: str | None = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        import FSEvents
        from Foundation import NSDate, NSRunLoop

        def callback(stream, info, num, event_paths, event_flags, event_ids):
            batch = []
            for i in range(num):
                path = str(event_paths[i])
                if _denied(path):
                    continue
                batch.append((path, int(event_flags[i])))
            if batch:
                self._on_batch(batch)

        stream = FSEvents.FSEventStreamCreate(
            None, callback, None, [str(self._root)],
            FSEvents.kFSEventStreamEventIdSinceNow, FSEVENTS_LATENCY,
            FSEvents.kFSEventStreamCreateFlagFileEvents
            | FSEvents.kFSEventStreamCreateFlagUseCFTypes
            | FSEvents.kFSEventStreamCreateFlagNoDefer,
        )
        if stream is None:
            self.failed = "FSEventStreamCreate returned NULL"
            self._on_batch(None)  # wake the consumer so the failure surfaces
            return
        FSEvents.FSEventStreamSetExclusionPaths(stream, _exclusions(self._root))
        FSEvents.FSEventStreamScheduleWithRunLoop(
            stream,
            FSEvents.CFRunLoopGetCurrent(),
            FSEvents.kCFRunLoopDefaultMode,
        )
        if not FSEvents.FSEventStreamStart(stream):
            self.failed = "FSEventStreamStart failed"
            FSEvents.FSEventStreamInvalidate(stream)
            FSEvents.FSEventStreamRelease(stream)
            self._on_batch(None)  # wake the consumer so the failure surfaces
            return
        rl = NSRunLoop.currentRunLoop()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_SLICE))
                elapsed = time.monotonic() - t0
                if elapsed < RUNLOOP_SLICE:
                    self._stop.wait(RUNLOOP_SLICE - elapsed)
        finally:
            FSEvents.FSEventStreamStop(stream)
            FSEvents.FSEventStreamInvalidate(stream)
            FSEvents.FSEventStreamRelease(stream)


async def run() -> None:
    try:
        import FSEvents
    except ImportError as e:
        print(f"[cowork-files] pyobjc FSEvents missing ({e!r}); driver idle", flush=True)
        return
    global _FLAG_NAMES
    _FLAG_NAMES = [
        (int(FSEvents.kFSEventStreamEventFlagItemCreated), "created"),
        (int(FSEvents.kFSEventStreamEventFlagItemRemoved), "removed"),
        (int(FSEvents.kFSEventStreamEventFlagItemRenamed), "renamed"),
        (int(FSEvents.kFSEventStreamEventFlagItemModified), "modified"),
    ]
    common.STATE_DIR.mkdir(parents=True, exist_ok=True)
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)  # real home, never $HOME
    # FDA detection is indirect: probe a TCC-protected subtree (excluded from
    # the watch anyway). Unreadable => coverage is partial. Warn once, keep going.
    try:
        os.listdir(home / "Library" / "Mail")
    except PermissionError:
        print(
            "[cowork-files] Full Disk Access not granted — file coverage is "
            "partial until it is (System Settings > Privacy & Security > "
            "Full Disk Access)",
            flush=True,
        )
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_batch(batch):
        try:
            loop.call_soon_threadsafe(q.put_nowait, batch)
        except RuntimeError:
            pass  # loop closing

    watcher = _StreamWatcher(home, on_batch)
    watcher.start()
    print(f"[cowork-files] watching {home} (FSEvents, per-file)", flush=True)
    try:
        while True:
            batch = await q.get()
            if batch is None or watcher.failed:
                print(f"[cowork-files] stream failed: {watcher.failed}", flush=True)
                return
            if not common.capture_enabled("files"):
                continue
            for path, flags in batch:
                change = _change_names(flags)
                if not change:
                    continue  # metadata-only churn (xattr/inode-meta/owner)
                payload = {"ts": common.now_iso(), "path": path, "change": change}
                if "created" in change or "renamed" in change:
                    url = await asyncio.to_thread(_source_url, path)
                    if url:
                        payload["source_url"] = url
                common.append_ndjson(FILE_LOG, payload)
                P.emit_event({"source": "cowork", "kind": "file_changed", **payload})
    except asyncio.CancelledError:
        print("[cowork-files] stopped", flush=True)
        raise
    finally:
        watcher.stop()
