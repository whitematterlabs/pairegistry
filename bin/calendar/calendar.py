#!/usr/bin/env python
"""
calendar — list and add events on the user's macOS calendars (EventKit).

Usage:
  calendar --list [--days N]                 # default N=7
  calendar --add "title" --when "datetime" [--duration MINUTES] [--calendar NAME]

datetime is anything dateutil can parse (e.g. "tomorrow 3pm",
"2026-05-10 14:00", "Friday 9am").
"""
from __future__ import annotations
import argparse, json, subprocess, sys, tempfile, os
from datetime import datetime, timedelta

SWIFT_LIST = r'''
import EventKit
import Foundation

let argv = CommandLine.arguments
guard argv.count >= 2, let days = Int(argv[1]) else {
    FileHandle.standardError.write("usage: list <days>\n".data(using: .utf8)!)
    exit(64)
}

let store = EKEventStore()
let sema = DispatchSemaphore(value: 0)
var granted = false
var authErr: Error? = nil
if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { g, e in granted = g; authErr = e; sema.signal() }
} else {
    store.requestAccess(to: .event) { g, e in granted = g; authErr = e; sema.signal() }
}
if sema.wait(timeout: .now() + 8) == .timedOut {
    FileHandle.standardError.write("calendar: auth request timed out\n".data(using: .utf8)!)
    exit(2)
}
guard granted else {
    FileHandle.standardError.write("calendar: access denied. Grant Calendar access to Terminal/your shell in System Settings → Privacy & Security → Calendars.\n".data(using: .utf8)!)
    if let e = authErr { FileHandle.standardError.write("  detail: \(e)\n".data(using: .utf8)!) }
    exit(3)
}

let now = Date()
let end = now.addingTimeInterval(Double(days) * 86400)
let pred = store.predicateForEvents(withStart: now, end: end, calendars: nil)
let events = store.events(matching: pred).sorted { $0.startDate < $1.startDate }

let df = DateFormatter()
df.dateFormat = "yyyy-MM-dd HH:mm"
df.timeZone = TimeZone.current

var out: [[String: Any]] = []
for e in events {
    var row: [String: Any] = [:]
    row["start"] = df.string(from: e.startDate)
    row["end"] = df.string(from: e.endDate)
    row["title"] = e.title ?? ""
    row["calendar"] = e.calendar.title
    row["all_day"] = e.isAllDay
    row["location"] = e.location ?? ""
    out.append(row)
}
let data = try JSONSerialization.data(withJSONObject: out, options: [])
FileHandle.standardOutput.write(data)
'''

SWIFT_ADD = r'''
import EventKit
import Foundation

// argv: title, isoStart, isoEnd, calendarName-or-empty
let argv = CommandLine.arguments
guard argv.count >= 5 else {
    FileHandle.standardError.write("usage: add <title> <iso-start> <iso-end> <calendar|empty>\n".data(using: .utf8)!)
    exit(64)
}
let title = argv[1]
let isoStart = argv[2]
let isoEnd = argv[3]
let calName = argv[4]

let iso = ISO8601DateFormatter()
iso.formatOptions = [.withInternetDateTime]
guard let startDate = iso.date(from: isoStart), let endDate = iso.date(from: isoEnd) else {
    FileHandle.standardError.write("calendar: bad iso datetime\n".data(using: .utf8)!)
    exit(64)
}

let store = EKEventStore()
let sema = DispatchSemaphore(value: 0)
var granted = false
if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { g, _ in granted = g; sema.signal() }
} else {
    store.requestAccess(to: .event) { g, _ in granted = g; sema.signal() }
}
if sema.wait(timeout: .now() + 8) == .timedOut {
    FileHandle.standardError.write("calendar: auth request timed out\n".data(using: .utf8)!)
    exit(2)
}
guard granted else {
    FileHandle.standardError.write("calendar: access denied. Grant Calendar access in System Settings → Privacy & Security → Calendars.\n".data(using: .utf8)!)
    exit(3)
}

let cals = store.calendars(for: .event)
var target: EKCalendar? = nil
if !calName.isEmpty {
    target = cals.first(where: { $0.title == calName })
    if target == nil {
        FileHandle.standardError.write("calendar: no calendar named '\(calName)'. Available: \(cals.map{$0.title}.joined(separator: ", "))\n".data(using: .utf8)!)
        exit(4)
    }
} else {
    target = store.defaultCalendarForNewEvents
    if target == nil { target = cals.first }
}
guard let cal = target else {
    FileHandle.standardError.write("calendar: no writable calendar found\n".data(using: .utf8)!)
    exit(5)
}

let ev = EKEvent(eventStore: store)
ev.title = title
ev.startDate = startDate
ev.endDate = endDate
ev.calendar = cal

do {
    try store.save(ev, span: .thisEvent, commit: true)
    let df = DateFormatter()
    df.dateFormat = "yyyy-MM-dd HH:mm"
    df.timeZone = TimeZone.current
    print("added: \(title) @ \(df.string(from: startDate)) (\(cal.title))")
} catch {
    FileHandle.standardError.write("calendar: save failed: \(error)\n".data(using: .utf8)!)
    exit(6)
}
'''


def run_swift(src: str, args: list[str]) -> tuple[int, bytes, bytes]:
    with tempfile.NamedTemporaryFile("w", suffix=".swift", delete=False) as f:
        f.write(src)
        path = f.name
    try:
        p = subprocess.run(
            ["swift", path, *args],
            capture_output=True,
            timeout=30,
        )
        return p.returncode, p.stdout, p.stderr
    finally:
        try: os.unlink(path)
        except OSError: pass


def parse_when(s: str) -> datetime:
    # Try dateutil if available; fall back to a couple of common formats.
    try:
        from dateutil import parser as duparser  # type: ignore
        return duparser.parse(s, fuzzy=True)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
            try: return datetime.strptime(s, fmt)
            except ValueError: continue
        raise SystemExit(f"calendar: can't parse --when '{s}'. Try '2026-05-10 14:00' or install python-dateutil.")


def cmd_list(days: int) -> int:
    rc, out, err = run_swift(SWIFT_LIST, [str(days)])
    if rc != 0:
        sys.stderr.write(err.decode("utf-8", "replace"))
        return rc
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        sys.stderr.write("calendar: bad JSON from helper\n")
        sys.stderr.write(out.decode("utf-8", "replace"))
        return 1
    if not rows:
        print(f"(no events in next {days} day{'s' if days != 1 else ''})")
        return 0
    for r in rows:
        marker = "ALL-DAY" if r.get("all_day") else r["start"]
        line = f"[{marker}] {r['title']} ({r['calendar']})"
        if r.get("location"): line += f" @ {r['location']}"
        print(line)
    return 0


def cmd_add(title: str, when: str, duration: int, calname: str) -> int:
    start = parse_when(when)
    end = start + timedelta(minutes=duration)
    iso_start = start.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    iso_end   = end.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    # Insert ":" in the timezone offset for ISO8601 parser compatibility
    def fix(z: str) -> str: return z[:-2] + ":" + z[-2:]
    rc, out, err = run_swift(SWIFT_ADD, [title, fix(iso_start), fix(iso_end), calname])
    sys.stdout.write(out.decode("utf-8", "replace"))
    sys.stderr.write(err.decode("utf-8", "replace"))
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(prog="calendar", description="list and add macOS calendar events")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list upcoming events")
    g.add_argument("--add", metavar="TITLE", help="create a new event with this title")
    ap.add_argument("--days", type=int, default=7, help="for --list: window in days (default 7)")
    ap.add_argument("--when", help="for --add: event start datetime")
    ap.add_argument("--duration", type=int, default=60, help="for --add: minutes (default 60)")
    ap.add_argument("--calendar", default="", help="for --add: target calendar name (default: system default)")
    a = ap.parse_args()

    if a.list:
        return cmd_list(max(1, a.days))
    if a.add:
        if not a.when:
            ap.error("--add requires --when")
        return cmd_add(a.add, a.when, max(1, a.duration), a.calendar)
    return 2


if __name__ == "__main__":
    sys.exit(main())
