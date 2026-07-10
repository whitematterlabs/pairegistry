#!/usr/bin/env python
"""write_calendar — create an event in the owner's Apple Calendar via EventKit.

Usage:
    write_calendar TITLE START END [--notes NOTES] [--calendar NAME]

    START / END are "YYYY-MM-DD HH:MM" in the owner's local timezone, e.g.
    write_calendar "Dinner with Alice" "2026-05-16 19:00" "2026-05-16 21:00" \
        --notes "Bring wine" --calendar Work

Reading the calendar (`cal`) is always allowed; *writing* is gated by the
owner's `capabilities.calendar_write` grant, read live from
`config.capability_modes()`. Unless it is `yes`, this command refuses and writes
nothing — the same source that gates the driver and the PAI's <capabilities>
block, so what the PAI is told and what it can do never drift.

EventKit (not AppleScript) does the write, matching the read path in `cal` and
avoiding a `tell application "Calendar"` Apple Event that would run on the app's
main thread.
"""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime

from EventKit import EKEntityTypeEvent, EKEvent, EKEventStore, EKSpanThisEvent
from Foundation import NSDate

from boot import config


def _request_access(store: EKEventStore) -> bool:
    done = threading.Event()
    result = {"granted": False}

    def handler(granted, err):
        result["granted"] = bool(granted)
        done.set()

    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, handler)

    done.wait(timeout=30)
    return result["granted"]


def _nsdate(value: str, label: str) -> NSDate:
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        _die(f"{label} is not in 'YYYY-MM-DD HH:MM' format: {value!r}")
    return NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())


def _die(msg: str) -> None:
    print(f"write_calendar: error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _pick_calendar(store: EKEventStore, name: str | None):
    if not name:
        cal = store.defaultCalendarForNewEvents()
        if cal is None:
            _die("no default calendar available for new events")
        return cal
    for cal in store.calendarsForEntityType_(EKEntityTypeEvent) or []:
        if cal.title() == name:
            if not cal.allowsContentModifications():
                _die(f"calendar {name!r} is read-only")
            return cal
    _die(f"no writable calendar named {name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="write_calendar",
        description="Create an event in the owner's Apple Calendar (EventKit).",
    )
    parser.add_argument("title", metavar="TITLE", help="event title")
    parser.add_argument("start", metavar="START", help='start "YYYY-MM-DD HH:MM"')
    parser.add_argument("end", metavar="END", help='end "YYYY-MM-DD HH:MM"')
    parser.add_argument("--notes", help="notes/description for the event")
    parser.add_argument("--calendar", help="target calendar name (default: default calendar)")
    args = parser.parse_args()

    # Gate: refuse unless the owner granted calendar writes. Read live so a
    # console toggle takes effect on the next invocation without a restart.
    mode = config.capability_modes().get("calendar_write", "no")
    if mode != "yes":
        _die(
            "calendar writes are off (capabilities.calendar_write="
            f"{mode}). Ask the owner to enable Calendar writes in the console; "
            "reading with `cal` still works."
        )

    start = _nsdate(args.start, "START")
    end = _nsdate(args.end, "END")
    if end.timeIntervalSince1970() <= start.timeIntervalSince1970():
        _die("END must be after START")

    store = EKEventStore.alloc().init()
    if not _request_access(store):
        _die(
            "calendar access denied. Grant access in System Settings > "
            "Privacy & Security > Calendars."
        )

    event = EKEvent.eventWithEventStore_(store)
    event.setTitle_(args.title)
    event.setStartDate_(start)
    event.setEndDate_(end)
    if args.notes:
        event.setNotes_(args.notes)
    event.setCalendar_(_pick_calendar(store, args.calendar))

    ok, err = store.saveEvent_span_error_(event, EKSpanThisEvent, None)
    if not ok:
        _die(f"could not save event: {err}")

    where = f' in calendar "{args.calendar}"' if args.calendar else ""
    print(f'Created event "{args.title}" from {args.start} to {args.end}{where}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
