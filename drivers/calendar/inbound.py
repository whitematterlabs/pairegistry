"""Apple Calendar inbound driver.

Subscribes to `EKEventStoreChangedNotification` on a background
`NSRunLoop` thread (PyObjC). On each notification, rescans the upcoming
HORIZON_DAYS window via EventKit, diffs against the cached snapshot in
`/sys/drivers/calendar/state.json`, and emits one `calendar:new` /
`calendar:changed` / `calendar:removed` event per affected item.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from boot import paths
from boot import processes as P

STATE_DIR = paths.PAI_ROOT / "sys" / "drivers" / "calendar"
STATE_PATH = STATE_DIR / "state.json"

# How far ahead the driver tracks events. Anything beyond rolls into the
# window naturally as time advances; the next refresh after midnight will
# pick up newly-in-horizon events as `calendar:new`.
HORIZON_DAYS = 30

# Slow safety-net refresh when no EKEventStoreChangedNotification has
# fired. Notifications are usually instant, so this is purely a backstop.
REFRESH_FALLBACK_SECONDS = 300


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
    for ent in new:
        P.emit_event({"source": "calendar", "kind": "new", **ent})
        print(f"[calendar-in] new: {ent['uid']} {ent['title']!r} @ {ent['start']}", flush=True)
    for ent in changed:
        P.emit_event({"source": "calendar", "kind": "changed", **ent})
        print(f"[calendar-in] changed: {ent['uid']} {ent['title']!r}", flush=True)
    for ent in removed:
        P.emit_event({"source": "calendar", "kind": "removed", **ent})
        print(f"[calendar-in] removed: {ent['uid']} {ent['title']!r}", flush=True)


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
                NSObject, NSNotificationCenter, NSRunLoop, NSDate,
            )
        except ImportError as e:
            print(f"[calendar-in] runloop thread: PyObjC missing ({e})", flush=True)
            return

        on_change = self._on_change

        class _Observer(NSObject):
            def changed_(self, _note):
                try:
                    on_change()
                except Exception as exc:
                    print(f"[calendar-in] on_change error: {exc!r}", flush=True)

        observer = _Observer.alloc().init()
        self._observer = observer
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            observer, "changed:", "EKEventStoreChangedNotification", self._store,
        )
        rl = NSRunLoop.currentRunLoop()
        try:
            while not self._stop.is_set():
                rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))
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

    # Initial diff against whatever we cached last run. If state.json is
    # empty (first boot), every upcoming event emits as `calendar:new` —
    # that's intentional: it gives PAIs the full upcoming window to set
    # reminders against, without a separate bootstrap event kind.
    try:
        await _refresh()
    except Exception as e:
        print(f"[calendar-in] initial refresh error: {e!r}", flush=True)

    try:
        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=REFRESH_FALLBACK_SECONDS)
            except asyncio.TimeoutError:
                pass
            # Coalesce bursts: EventKit can fire several notifications back-to-back
            # while iCloud reconciles; one snapshot+diff covers them all.
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                await _refresh()
            except Exception as e:
                print(f"[calendar-in] refresh error: {e!r}", flush=True)
    except asyncio.CancelledError:
        raise
    finally:
        watcher.stop()
        print("[calendar-in] stopped", flush=True)
