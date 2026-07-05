"""Apple Calendar inbound driver.

Subscribes to `EKEventStoreChangedNotification` on a background
`NSRunLoop` thread (PyObjC). On each notification, rescans the upcoming
HORIZON_DAYS window via EventKit, diffs against the cached snapshot in
`/sys/drivers/calendar/state.json`, and emits one `calendar:changes`
event carrying `new`/`changed`/`removed` lists for the whole refresh.
Per-item events would fan out to one PAI nudge each, which on first
boot floods the bus; one event per refresh = one nudge.

Why EventKit and not `~/Library/Calendars/*.sqlitedb`: Apple changes the
Calendar SQLite schema between releases without notice, and EventKit
already coalesces local + iCloud + Exchange calendars into one store
with proper change notifications. The skill `author-driver` calls this
out explicitly.

Why a periodic safety-net refresh in addition to notifications: the
`EKEventStoreChangedNotification` runloop can be starved across system
suspend/resume, and EventKit itself sometimes drops notifications when
iCloud reconciles in the background. A slow `REFRESH_FALLBACK_SECONDS`
tick guarantees the diff catches up without flooding the bus.

Requires Calendar access for whichever process runs the kernel: System
Settings → Privacy & Security → Calendars. First launch will prompt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from boot import paths
from boot import processes as P

STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "calendar"
STATE_PATH = STATE_DIR / "state.json"

# How far ahead the driver tracks events. Anything beyond rolls into the
# window naturally as time advances; the next refresh after midnight will
# pick up newly-in-horizon events in the `new` list of `calendar:changes`.
HORIZON_DAYS = 3

# Slow safety-net refresh when no EKEventStoreChangedNotification has
# fired. Notifications are usually instant, so this is purely a backstop.
REFRESH_FALLBACK_SECONDS = 300

# Floor between EventKit snapshots. On a *cold* boot CalendarDaemon streams
# the whole store in from iCloud/Exchange and fires EKEventStoreChangedNotification
# in a sustained storm — hundreds of notifications, most yielding an empty diff.
# Without a floor the loop runs a full snapshot per notification back-to-back and
# pins a core at ~100% for the duration of the warmup. This caps snapshots to one
# per interval; a storm collapses to ~one refresh every few seconds.
MIN_REFRESH_INTERVAL = 3.0

# After a wake, wait this long for the burst to finish arriving before snapshotting,
# so a multi-notification change (iCloud reconcile) coalesces into a single diff.
SETTLE_DELAY = 0.5


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


_TRACKED_FIELDS = (
    "title", "start", "end", "location",
    "calendar_name", "notes", "is_all_day",
)


def _entity_hash(entity: dict) -> str:
    payload = {k: entity.get(k) for k in _TRACKED_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _nsdate_to_iso(nsdate) -> Optional[str]:
    if nsdate is None:
        return None
    ts = nsdate.timeIntervalSince1970()
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def _ek_snapshot(store) -> dict[str, dict]:
    """Return {uid: entity-dict} for every EKEvent in the upcoming horizon."""
    from Foundation import NSDate

    start = NSDate.date()
    end = NSDate.dateWithTimeIntervalSinceNow_(HORIZON_DAYS * 24 * 3600)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        start, end, None
    )
    events = store.eventsMatchingPredicate_(predicate) or []
    snap: dict[str, dict] = {}
    for ev in events:
        uid = ev.calendarItemIdentifier()
        if uid is None:
            uid = ev.eventIdentifier()
        if not uid:
            continue
        uid = str(uid)
        title = ev.title()
        location = ev.location()
        notes = ev.notes()
        cal = ev.calendar()
        snap[uid] = {
            "uid": uid,
            "title": str(title) if title else "",
            "start": _nsdate_to_iso(ev.startDate()),
            "end": _nsdate_to_iso(ev.endDate()),
            "location": str(location) if location else None,
            "calendar_name": str(cal.title()) if cal and cal.title() else "",
            "notes": str(notes) if notes else None,
            "is_all_day": bool(ev.isAllDay()),
        }
    return snap


def _diff(prev: dict[str, dict], curr: dict[str, dict]):
    prev_ids = set(prev)
    curr_ids = set(curr)
    new = [curr[u] for u in sorted(curr_ids - prev_ids)]
    removed = [prev[u] for u in sorted(prev_ids - curr_ids)]
    changed: list[dict] = []
    for u in sorted(prev_ids & curr_ids):
        if _entity_hash(prev[u]) != _entity_hash(curr[u]):
            changed.append(curr[u])
    return new, changed, removed


def _emit_diff(new: list[dict], changed: list[dict], removed: list[dict]) -> None:
    P.emit_event({
        "source": "calendar",
        "kind": "changes",
        "new": new,
        "changed": changed,
        "removed": removed,
    })
    print(
        f"[calendar-in] changes: new={len(new)} changed={len(changed)} removed={len(removed)}",
        flush=True,
    )


def _request_access(store) -> bool:
    """Block (up to 30s) on the user's calendar-access decision."""
    from EventKit import EKEntityTypeEvent

    done = threading.Event()
    granted: dict = {"v": False}

    def cb(ok, err):
        granted["v"] = bool(ok)
        if err is not None:
            print(f"[calendar-in] access callback error: {err}", flush=True)
        done.set()

    # macOS 14+ split read vs write access; older systems use the unified call.
    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(cb)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, cb)
    done.wait(timeout=30)
    return granted["v"]


_OBSERVER_CLASS = None


def _observer_class():
    """Lazily define (once per process) the NSObject subclass that observes
    EKEventStoreChangedNotification.

    An Objective-C class name is registered in a *process-global* runtime
    table, so defining the subclass a second time raises
    ``objc.error: <name> is overriding existing Objective-C class``. The
    watcher thread is (re)started on every driver stop+start, so the class
    definition MUST NOT live inside the thread body — otherwise the second
    and every later start crashes the runloop thread before it can register
    the observer, silently deafening the driver to real-time changes (only
    the slow fallback survives). Define once, cache, reuse.

    The per-instance callback can't be closed over (the class is shared), so
    it's stashed as a plain Python attribute on the instance — PyObjC allows
    this on Python-defined subclasses.
    """
    global _OBSERVER_CLASS
    if _OBSERVER_CLASS is None:
        from Foundation import NSObject

        class _CalendarChangeObserver(NSObject):
            def changed_(self, _note):
                cb = getattr(self, "_on_change", None)
                if cb is None:
                    return
                try:
                    cb()
                except Exception as exc:
                    print(f"[calendar-in] on_change error: {exc!r}", flush=True)

        _OBSERVER_CLASS = _CalendarChangeObserver
    return _OBSERVER_CLASS


class _ChangeWatcher:
    """Background NSRunLoop thread observing EKEventStoreChangedNotification.

    EventKit posts the notification on whichever thread the change was
    triggered from; we observe on this dedicated thread's runloop so we
    don't need to be on the main thread. The runloop is pumped in 1s
    slices so cancellation latency is bounded.
    """

    def __init__(self, store, on_change: Callable[[], None]):
        self._store = store
        self._on_change = on_change
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._observer = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="calendar-runloop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            from Foundation import (
                NSNotificationCenter, NSRunLoop, NSDate,
            )
        except ImportError as e:
            print(f"[calendar-in] runloop thread: PyObjC missing ({e})", flush=True)
            return

        observer = _observer_class().alloc().init()
        observer._on_change = self._on_change
        self._observer = observer
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            observer, "changed:", "EKEventStoreChangedNotification", self._store,
        )
        rl = NSRunLoop.currentRunLoop()
        # Bound the loop so it cannot busy-spin. With a live EKEventStore
        # observer attached, `runUntilDate_` returns *immediately* instead of
        # blocking for the slice (observed: the loop then pins a core at ~100%
        # marshalling the two ObjC calls per iteration through PyObjC — the
        # actual cold-boot spin). A bare runloop with no store blocks fine,
        # which is why it only shows up once EventKit is wired up. Whatever
        # CoreFoundation is doing internally, we refuse to iterate faster than
        # RUNLOOP_SLICE: if runUntilDate_ returned early, wait out the rest of
        # the slice on the stop event (so cancellation stays instant). Pending
        # EKEventStoreChangedNotifications queue on the mach port and are
        # serviced on the next slice — coalescing bursts, not dropping them.
        RUNLOOP_SLICE = 1.0
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(RUNLOOP_SLICE))
                elapsed = time.monotonic() - t0
                if elapsed < RUNLOOP_SLICE:
                    self._stop.wait(RUNLOOP_SLICE - elapsed)
        finally:
            NSNotificationCenter.defaultCenter().removeObserver_(observer)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)


async def run() -> None:
    try:
        from EventKit import EKEventStore
    except ImportError as e:
        print(
            f"[calendar-in] EventKit not importable ({e}); driver idle. "
            "Install with: pip install pyobjc-framework-EventKit",
            flush=True,
        )
        return

    store = EKEventStore.alloc().init()
    granted = await asyncio.to_thread(_request_access, store)
    if not granted:
        print(
            "[calendar-in] calendar access denied or undecided; driver idle. "
            "Enable in System Settings → Privacy & Security → Calendars, then restart.",
            flush=True,
        )
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_change() -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, "change")
        except RuntimeError:
            # Loop is closing — drop the wakeup; the next process boot rediffs.
            pass

    watcher = _ChangeWatcher(store, on_change)
    watcher.start()
    print("[calendar-in] started; horizon=%dd, fallback=%ds" % (
        HORIZON_DAYS, REFRESH_FALLBACK_SECONDS), flush=True)

    state: dict = _load_state()

    async def _refresh() -> None:
        snap = await asyncio.to_thread(_ek_snapshot, store)
        new, changed, removed = _diff(state, snap)
        if new or changed or removed:
            _emit_diff(new, changed, removed)
            state.clear()
            state.update(snap)
            await asyncio.to_thread(_save_state, state)

    def _drain() -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    # Initial diff against whatever we cached last run. On first boot
    # state.json is empty, so the full upcoming window lands in the `new`
    # list of a single `calendar:changes` event — one nudge, full context.
    try:
        await _refresh()
    except Exception as e:
        print(f"[calendar-in] initial refresh error: {e!r}", flush=True)

    last_refresh = time.monotonic()
    absorbed = 0  # notifications swallowed by the rate floor since last log
    try:
        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=REFRESH_FALLBACK_SECONDS)
            except asyncio.TimeoutError:
                pass
            # Let a burst finish landing before we snapshot: EventKit fires
            # several notifications back-to-back while iCloud reconciles.
            await asyncio.sleep(SETTLE_DELAY)
            _drain()
            # Rate floor: on a cold boot the store streams in as a sustained
            # notification storm. Without this, each notification triggers a
            # full EventKit snapshot back-to-back and pins a core at ~100%.
            # Cap snapshots to one per MIN_REFRESH_INTERVAL; keep draining
            # wakeups that arrive while we wait out the floor.
            since = time.monotonic() - last_refresh
            if since < MIN_REFRESH_INTERVAL:
                absorbed += 1
                await asyncio.sleep(MIN_REFRESH_INTERVAL - since)
                _drain()
            if absorbed and absorbed % 50 == 0:
                print(
                    f"[calendar-in] rate floor active: absorbed {absorbed} "
                    "back-to-back change bursts (cold-boot storm)",
                    flush=True,
                )
            try:
                await _refresh()
            except Exception as e:
                print(f"[calendar-in] refresh error: {e!r}", flush=True)
            last_refresh = time.monotonic()
    except asyncio.CancelledError:
        raise
    finally:
        watcher.stop()
        print("[calendar-in] stopped", flush=True)
