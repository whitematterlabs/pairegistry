---
name: author-driver
visible_to: [root]
description: Howto for creating a new driver — three-location split, events.yaml manifest, filesystem-based kernel discovery, deploy flow. Reference when scaffolding a new event source.
---

# Authoring a driver

**Stop — did you classify?** Driver = *primitive external surface*
(messages, email, calendar, contacts) whose on-disk shape it owns.
If your need is a CLI that returns a value, close this file and
build a bin. If it's a task ("calendar booking", "reservations"),
build a bin on top of an existing primitive driver.

Background reading before authoring:
- `memory/doc/FILESYSTEM_v3.md` — `/usr/lib/drivers/<name>/`, `/sys/drivers/<name>/`, `/proc/<slug>/`
- `memory/doc/KERNEL_EVENTS.md` — event vocabulary, wake_on globs
- `memory/doc/KERNEL.md` — discovery + reconcile
- `memory/doc/PAIMAN.md` — install flow

A driver owns the on-disk shape of an external surface (messages,
email, calendar, contacts). The kernel routes its events but does
not interpret them.

## The three slots

| Slot | What you create | What you don't |
|---|---|---|
| `/usr/lib/drivers/<name>/` | code + `events.yaml` + `package.yaml` | runtime state |
| `/sys/drivers/<name>/` | (created at runtime by the driver) | code |
| `/proc/<slug>/` | (created at runtime by the kernel) | code or runtime state |

There is **no `/etc/drivers/`**. The kernel discovers drivers by
scanning `/usr/lib/drivers/*/events.yaml` at boot — no code
registration needed. Install the package, reboot the kernel so new
`wake_on:` globs in `/etc/config.yaml` are picked up.

**Drivers ≠ PAIs.** A PAI is an instance with `/home/<pai>/`,
`/var/lib/instances/<pai>/`, and `/proc/<pai>/`. A driver has no
home, no instance state — only code (`/usr/lib/drivers/<name>/`),
runtime state (`/sys/drivers/<name>/`), and process lifecycle
(`/proc/<slug>/`). Drivers emit events; PAIs consume them.

## Where the source lives: pairegistry vs local

Two valid origins for a driver. Pick deliberately.

| | pairegistry (`~/Projects/pairegistry/drivers/<name>/`) | local (`~/.pai/usr/lib/drivers/<name>/` direct) |
|---|---|---|
| Use when | Driver is general-purpose, will run on other PAI installs, deserves a version | Driver is the owner's specific situation: a quirky local API, a personal bridge, an experiment |
| Install path | `paiman install <name>` (symlinks into `/usr/lib/drivers/`) | Author the directory in place |
| Self-healing edits | Edit pairegistry source, reinstall | Edit `/usr/lib/drivers/<name>/` directly |
| Reviewability | Has its own git history, package.yaml versioning | Lives only on this machine |

Default to **pairegistry** for anything you'd describe to a stranger
("an iMessage driver", "a Gmail driver"). Default to **local** for
anything tied to the owner's specific setup (a WhatsApp scrape that
depends on their ChatStorage layout). When in doubt: local first,
promote to pairegistry once it stabilizes.

What you must **never** do is mix them — don't author a real
directory at `~/.pai/usr/lib/drivers/<name>/` while a pairegistry
copy of the same name exists. The symlink/real-dir split makes
`paiman install` ambiguous and edits land in the wrong place.

## Package layout

```
/usr/lib/drivers/<name>/
├── package.yaml        # name, kind: driver, version, description
├── events.yaml         # event vocabulary + process registry
├── __init__.py
├── inbound.py          # if the driver emits events (e.g. iMessage in)
└── outbound.py         # if the driver consumes events (e.g. iMessage out)
```

A driver may have either or both halves — `inbound`/`outbound` are
conventional, not required by name. The split is reflected in the
slug: process slugs are `<name>-in` / `<name>-out`; the package
name (under `/usr/lib/drivers/`) omits the suffix.

When authoring via `execute-claudecode`, set `type: driver` in the
brief — claudecode will write to `~/Projects/pairegistry/drivers/<name>/`
(or `/usr/lib/drivers/<name>/` for a local-only driver).

## events.yaml manifest

Two top-level sections: `processes:` (what the kernel runs) and
`events:` (the routing vocabulary).

```yaml
driver: imessage
description: Inbound and outbound iMessage routing.

processes:
  - slug: imessage-in              # what /proc/<slug>/ will be called
    module: drivers.imessage.inbound   # importable Python module path
    entrypoint: run                # async function to call (default: run)
  - slug: imessage-out
    module: drivers.imessage.outbound
    entrypoint: run

events:
  - kind: imessage:new          # the routing key — what wake_on matches
    description: A new message arrived from a contact.
    emitted_by: drivers/imessage/inbound.py
    raw_kind: new_message       # the YAML `kind:` field on the event file
    payload:
      thread: string             # contact slug
      sender: string             # "me" if from_me, else contact slug
      text: string
      day_file: string           # relative path to the day's .md file

  - kind: imessage:owner
    description: Owner sent a message to PAI via the TUI.
    ...
```

**`processes:`** — the kernel walks every driver's `events.yaml` at
boot, collects this list, and supervises one asyncio task per entry
(see "Driver runtime contract" below). Omit the section for
library-only drivers (e.g. `contacts/`, `messages/`) that don't run
their own process.

**`events:`** — `kind` is what `wake_on:` globs match. `raw_kind` is
the YAML `kind:` field on the event file dropped into
`/run/pai/events/`. Often the same; the distinction matters only when
a driver emits multiple routing kinds from one raw kind (or vice versa).

## Driver runtime contract

Every entry in `processes:` becomes a long-lived asyncio task. The
kernel's reconcile step calls `<module>.<entrypoint>()` to **build a
coroutine**, then schedules it under `_supervise_driver`. This means:

> **`entrypoint` MUST be `async def`. Calling it must return a
> coroutine immediately — never enter a loop, never block, never
> sleep synchronously.**

If `run` is plain `def` with a `while True: ... time.sleep(N)`
body, the call to `run()` enters the loop on the kernel's main
thread and never returns. Reconcile wedges, the supervise loop
never starts, and the entire fleet goes silent. Symptom in
`kernel.log`:

```
[kernel] driver started: <other-driver>
[<your-driver>] starting        ← your run() printed this, then blocked
                                  (no "driver started: <your-driver>" line follows)
```

### Don't backfill history. Start from "now."

A driver attaching to an existing surface (calendar, email, messages,
contacts, photos, files) **must not ingest the full history** on first
run. The owner has years of data; emitting one event per historical
item floods every PAI with `wake_on:` matching the kind, burns model
turns, fills `/var/spool/`, and produces zero value — those events
already happened, the owner has already dealt with them.

On first run the driver's job is to **establish a cursor at "now"**
and emit nothing for what came before. The cursor lives under
`/sys/drivers/<name>/` (e.g. `cursor.yaml` with `last_seen: <ISO ts>`
or `last_rowid: N`). If the file is missing, initialize it to the
current high-water mark and stop. From the next tick onward, emit
only items strictly newer than the cursor.

Concretely:

| Surface | First-run cursor |
|---|---|
| Calendar (EventKit) | `last_modified` watermark = `now`; only emit events whose `modificationDate > cursor` going forward. Do **not** iterate `predicateForEventsWithStartDate` over the past. |
| Email (IMAP/Mail) | `last_uid` per folder = current max UID. |
| iMessage (`chat.db`) | `last_rowid` = `SELECT MAX(ROWID) FROM message`. |
| Files (FSEvents) | Subscribe from now; never replay the directory. |
| HTTP webhook | No cursor — events arrive in real time by definition. |

If the owner explicitly asks for backfill ("import my last month of
calendar events"), that is a **one-shot bin invocation**, not driver
boot behavior. The driver still starts at `now`; the bin emits the
historical batch on demand with a bounded window.

Symptom of getting this wrong: the kernel log shows hundreds-to-
thousands of `<driver>:<kind>` events in the first seconds after
`paictl start`, every consumer PAI's `/proc/<pai>/log.md` is buried,
and the owner's inbox fills with stale-looking nudges. If you see this
during verify, **stop the driver, fix the cursor, wipe
`/sys/drivers/<name>/`**, and restart.

### Event-volume budget — batch, don't fan out.

A driver emits **one event per real-world change**, not one event
per affected item. If a single sync tick discovers N new/changed
items, that is **one** event whose payload carries the list — not N
events. The kernel routes every emitted event to every PAI whose
`wake_on:` matches; N events = N model turns = N nudges in
`/proc/<pai>/log.md`. Fan-out at emit time multiplies straight
into the owner's attention.

Rule of thumb: **if your driver could plausibly emit >10 events
in <1 second, you're doing it wrong — batch.** Reconciling a
calendar refresh, draining a backlog of new messages, replaying a
file scan: all of these are *one* `<surface>:changes` event with
`new: [...]`, `changed: [...]`, `removed: [...]` lists in the
payload. The consuming PAI iterates the lists; the kernel sees a
single nudge.

Correct shape:

```python
P.emit_event({
    "kind": "calendar:changes",
    "new":     [...],   # list of items, possibly large
    "changed": [...],
    "removed": [...],
})
```

Wrong shape (fan-out flood):

```python
for item in new_items:
    P.emit_event({"kind": "calendar:item_added", "item": item})  # N nudges
```

Symptom of getting this wrong: at `paictl start <slug>` the kernel
log shows dozens-to-hundreds of `<driver>:*` events in the first
seconds, the owner gets a wall of nudges for things they already
knew about, and consumer PAIs' `/proc/<pai>/log.md` is buried. If
you see this during verify, stop the driver and collapse the loop
into a single batched emit before restarting.

This composes with the cursor rule above: cursor avoids emitting
*historical* items at all; batching ensures that even legitimate
"many things changed at once" surfaces (a calendar refresh after
sleep, an email folder reindex) still cost the kernel exactly one
event.

### Don't poll. Subscribe.

PAI is an event-driven system. A driver that wakes up every N seconds
to ask "anything new?" is wrong by default — it burns power, adds
latency floor equal to the poll interval, and wedges other drivers
during slow ticks. Only fall back to polling when no notification
mechanism exists, and even then justify it in a comment.

By signal type:

| Surface | Use | Don't use |
|---|---|---|
| File / directory changes | `watchdog` (FSEvents on macOS, inotify on Linux) | `os.path.getmtime` in a loop |
| Subprocess output (Node bridge, tail, etc.) | `asyncio.create_subprocess_exec` + `await proc.stdout.readline()` | `Popen` + `read()` in a loop |
| SQLite changes | `sqlite3` update hooks via `sqlite3_update_hook` (in C extensions) or `watchdog` on the DB file's `-wal` | `SELECT … WHERE id > cursor` in a poll loop |
| HTTP / external services | webhook → server in driver, OR provider's streaming/long-poll endpoint (Gmail push, IMAP IDLE, etc.) | scheduled `requests.get` |
| macOS app state | `NSDistributedNotificationCenter`, `CFNotificationCenter`, `NSWorkspace` notifications, AX observers | re-querying a UI tree every second |
| iMessage (`chat.db`) | `watchdog.Observer` on the real macOS user's `Library/Messages/chat.db-wal` + `chat.db-shm` | `SELECT MAX(ROWID) FROM message` in a loop |

If the only available interface is polling (third-party API with no
push, sqlite without WAL, etc.), the rule is: **slowest interval
that meets the product requirement**, never sub-5-second by default,
and always wrapped in `asyncio.to_thread` so the event loop stays
free.

### Skeleton — correct (FSEvents-driven)

```python
import asyncio
import os
import pwd
from pathlib import Path
from boot import processes as P
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# The kernel overrides $HOME per PAI, so never use Path.home() for host
# ~/Library data. Resolve the real macOS account home via getpwuid.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
WATCH_PATH = REAL_HOME / "Library" / "Messages" / "chat.db-wal"

async def run() -> None:
    print("[mydriver] starting", flush=True)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    class Handler(FileSystemEventHandler):
        def on_modified(self, event):
            loop.call_soon_threadsafe(queue.put_nowait, event.src_path)

    observer = Observer()
    observer.schedule(Handler(), str(WATCH_PATH.parent), recursive=False)
    observer.start()
    try:
        while True:
            await queue.get()
            try:
                await _drain_new_rows()    # emits events via P.emit_event
            except Exception as e:
                print(f"[mydriver] drain error: {e!r}", flush=True)
    except asyncio.CancelledError:
        raise
    finally:
        observer.stop()
        observer.join(timeout=2)
```

### Skeleton — wrong (polling + sync def + blocking sleep)

```python
import time

def run() -> None:                    # ← sync def: BUG
    print("[mydriver] starting", flush=True)
    while True:
        _tick()                       # ← polling instead of subscribing
        time.sleep(2)                 # ← blocks the event loop: BUG
```

## Non-Python artifacts (Node bridges, native helpers, etc.)

Some external surfaces (WhatsApp Web, browser automation, Discord
voice) only expose themselves through a non-Python ecosystem. Wrap
them in a sidecar process the driver supervises — never embed JS/Go
sources next to your Python.

PAI follows FHS conventions: Python driver code lives in
`/usr/lib/drivers/<name>/`, **non-Python sidecar helpers live in
`/usr/libexec/<name>/`**. This is the same split Debian, Homebrew,
and most distros use for "internal helper a program execs but the
user never calls directly".

**Layout — pairegistry source:**

```
~/Projects/pairegistry/drivers/<name>/
├── package.yaml          # declares libexec subdir + install step
├── events.yaml
├── __init__.py
├── inbound.py            # Python: owns events + on-disk shape
├── outbound.py           # Python: launches the bridge as a child
└── libexec/              # ← non-Python sidecar source ships here
    ├── package.json      # OR Cargo.toml, OR go.mod, etc.
    └── bridge.js
```

**Layout — installed:**

```
/usr/lib/drivers/<name>/        # Python only (paiman symlink)
├── package.yaml
├── events.yaml
├── __init__.py
├── inbound.py
└── outbound.py

/usr/libexec/<name>/             # paiman copies pairegistry's libexec/ here
├── package.json
├── bridge.js
└── node_modules/                # populated at install time, NOT committed
```

`paiman install` lays out both halves: the Python tree gets symlinked
into `/usr/lib/drivers/<name>/`, the `libexec/` subdir is installed
to `/usr/libexec/<name>/`, then the declared install step (e.g.
`npm install --omit=dev`) runs there to populate dependencies.

Rules:

1. **`/usr/lib/drivers/<name>/` is Python-only.** No `.js`, no
   `package.json`, no `node_modules/` at the driver root. If you find
   one there, it belongs in `/usr/libexec/<name>/`.

2. **Never commit `node_modules/`, `target/`, `vendor/`, etc. in
   pairegistry.** Add them to `.gitignore`. They get installed at
   deploy time, not at author time.

3. **Declare the install step in `package.yaml`** so paiman knows how
   to populate `/usr/libexec/<name>/`:

   ```yaml
   name: whatsapp
   kind: driver
   libexec:
     install: ["npm", "install", "--omit=dev"]   # argv list, run inside /usr/libexec/whatsapp/
   ```

4. **Resolve the runtime binary at startup, don't hard-code it.** Use
   `shutil.which("node")` (or the equivalent) and fail loud with a
   `print + return` if it's missing. Hard-coding `/opt/homebrew/bin/node`
   makes the driver brittle to Apple Silicon vs Intel vs Linux vs
   custom installs.

   ```python
   import shutil
   from boot import paths

   PAI_ROOT = paths.PAI_ROOT
   BRIDGE_JS = PAI_ROOT / "usr" / "libexec" / "whatsapp" / "bridge.js"
   NODE_BIN = shutil.which("node")

   if not NODE_BIN:
       print("[whatsapp] node binary not found on PATH; driver idle", flush=True)
       return
   if not BRIDGE_JS.exists():
       print(f"[whatsapp] bridge.js not found at {BRIDGE_JS}; driver idle", flush=True)
       return
   ```

5. **Supervise the sidecar with `asyncio.create_subprocess_exec`.**
   Read its stdout line-by-line in an async task; restart it with
   exponential backoff if it dies. The bridge owes you a JSON-per-line
   wire protocol — that's it.

6. **Bridge runtime state goes under `/sys/drivers/<name>/`** (auth
   tokens, QR session dirs, cached cookies). `/usr/libexec/` is the
   *code+deps* slot, not state. Same rule as Python state.

If you find yourself authoring more than ~200 lines of non-Python
code, stop and ask whether this should be its own package on the
language's ecosystem (an npm package, a brew formula) that the
driver depends on, rather than a vendored bridge.

### Blocking I/O inside an async driver

`requests`, sync `sqlite3`, `subprocess.run`, `os.read` on a regular
file, anything that calls into a C library that holds the GIL — these
freeze the event loop while they run. The kernel's other drivers
stop, the timer heap stops firing, nudges queue up.

Wrap synchronous I/O in `asyncio.to_thread` so the event loop stays
responsive:

```python
async def _send(text: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_send_sync, text)

def _send_sync(text: str) -> tuple[bool, str]:
    r = requests.post(BRIDGE_URL, json={"text": text}, timeout=30)
    return (r.ok, r.text)
```

For long-running subprocesses (a Node bridge, a tail of a log file),
prefer `asyncio.create_subprocess_exec` over `subprocess.Popen` — its
`stdout`/`stderr` are awaitable streams.

### Periodic work

Most drivers should be **subscription-driven**, not periodic — see
"Don't poll. Subscribe." above. When you genuinely need a timer
(e.g. a once-an-hour reconcile), use `await asyncio.sleep(N)`, never
`time.sleep(N)`. If you need multiple concurrent loops inside one
driver, spawn them with `asyncio.create_task` and `await` them with
`asyncio.gather`.

### Cancellation

The kernel cancels driver tasks on shutdown and on
`paictl stop <slug>`. Honor `asyncio.CancelledError`:

```python
async def run() -> None:
    try:
        while True:
            await _tick()
            await asyncio.sleep(POLL_INTERVAL)
    except asyncio.CancelledError:
        # last-chance cleanup: close sockets, flush cursors
        raise
```

### Reference implementations

- `~/Projects/pairegistry/drivers/imessage/inbound.py` — async loop, FSEvents watcher.
- `~/Projects/pairegistry/drivers/imessage/outbound.py` — async tailer + `osascript` via subprocess.
- `~/Projects/pairegistry/drivers/email/macmail/inbound.py` — async polling with `to_thread`.
- `~/Projects/pairegistry/drivers/email/package.yaml` — multi-subdriver namespace example.

## Emitting an event

A driver writes `kind: <raw_kind>` plus payload fields to a YAML
file at `/run/pai/events/{timestamp}-{source}-{slug}.yaml`. The
kernel's FS watcher picks it up, reads, deletes, routes.

From driver code (Python), call the in-process helper:

```python
from boot import processes as P

P.emit_event({
    "kind": "imessage:new",
    "thread": "kaia",
    "sender": "kaia",
    "text": "dinner thursday?",
})
```

`bin/send-message` is the *peer-to-peer* CLI for one PAI to message another
(`bin/send-message --to <pid> --content "..."`). It is not the driver emit
path — drivers run as kernel-supervised processes and have direct
access to `P.emit_event`.

## Deploying the driver

The kernel discovers drivers by scanning `/usr/lib/drivers/*/events.yaml`
at boot. The full deploy flow once the code is written:

```sh
# 1. Install — symlinks pairegistry source into /usr/lib/drivers/<name>/
paiman install <name>

# 2. Reboot so the kernel scans the new events.yaml
reboot

# 3. Activate the process(es)
paictl start <name>-in     # if inbound
paictl start <name>-out    # if outbound

# 4. Add wake_on globs to any PAI that should receive these events,
#    e.g. in /etc/config.yaml under the PAI entry:
#      wake_on: ["<name>:*"]
#    then `reboot` again so the new wake_on is picked up.
```

See skill `kernel-restart` for restart procedure and caveats.

## Runtime state

Whatever cursors / last-event watermarks / queue depth the driver
needs go under `/sys/drivers/<name>/`. The driver owns this dir.
Read-mostly for everyone else — it's the sysfs-style introspection
window.

## Becoming visible to a PAI — `home.links`

A driver's `package.yaml` may declare `home.links` (same shape as a pai
bundle's). When a PAI mounts this driver, the kernel stitches those
links into the PAI's home — that is how a consumer PAI sees the
on-disk surface the driver owns:

```yaml
name: email
kind: driver
home:
  links:
    - link: communication/email
      target: var/spool/communication/email
```

A PAI mounts this driver when it lists the driver in its bundle `deps:`
(or when it is the `fallback: true` PAI, which mounts every installed
driver). Bundleless PAIs like `root` mount no drivers. So:

- **Pick narrow link names.** They land in someone else's `$HOME`. A
  link name that collides with a bundle/seed link is a hard stitch-time
  error — you cannot shadow a bundle's path.
- A library-only driver with no external surface to expose (e.g.
  `contacts`, `messages`) can omit `home.links` entirely.

Full policy (the three mounting rules): `memory/doc/FILESYSTEM_v3.md` →
"Driver mounting".

## Prefer native APIs over raw DB access

Before reading a SQLite file directly, check whether the OS or a
well-maintained library already exposes the same data through a
stable API. Native APIs handle schema migrations, permissions, and
change notifications for you.

| Surface | Prefer | Over |
|---|---|---|
| macOS Calendar | `EventKit` via PyObjC (`EventKit.EKEventStore`) | `~/Library/Calendars/*.sqlitedb` |
| macOS Contacts | `Contacts.CNContactStore` via PyObjC | `~/Library/Application Support/AddressBook/` |
| macOS Reminders | `EventKit.EKEventStore` (same store as Calendar) | raw SQLite |
| macOS Mail | `~/Library/Mail/` MBOX files | Mail.app SQLite indexes |
| iOS/macOS Health | `HealthKit` (requires entitlement — skip unless owner grants) | `~/Library/Health/` SQLite |

**Decision rule:** if a native framework exists for the platform,
use it. Fall back to SQLite/file parsing only when:
- No native API exists (iMessage → `chat.db` is the only interface), or
- The API requires entitlements the process can't get, or
- The API is significantly slower than direct DB access for the required polling frequency (rare — and if you're polling, rethink the design first).

## When you don't know the right approach

If you're unsure how to reach the external surface:

1. **Web search first.** Query for the app or data source + "macOS API",
   "PyObjC", "python", "reverse engineer". Look for prior art — someone
   has likely hit the same wall.

2. **Try the Accessibility API.** Any app that renders UI is readable via
   `ApplicationServices.AXUIElement`. Use it when there's no data API but
   you can observe state or trigger actions through the app's UI:

   ```python
   from ApplicationServices import AXUIElementCreateApplication, AXUIElementCopyAttributeValue
   import AppKit
   ```

   Common use cases: scraping displayed data, driving a GUI app
   programmatically, listening for focus/selection changes.

3. **Other Apple developer APIs worth knowing:**
   - `NSWorkspace` — app launch/quit events, frontmost app, file associations
   - `CoreData` / `NSPersistentContainer` — read iCloud-backed stores
   - `FSEvents` via `watchdog` — low-latency filesystem change notifications
   - `ScriptingBridge` — AppleScript-over-Python for scriptable apps
   - `CFNotificationCenter` / `NSDistributedNotificationCenter` — inter-app
     broadcast events (e.g. media player state)

4. **Check the app's own IPC.** Many apps expose XPC services, UNIX sockets,
   or named pipes under `~/Library/` or `/tmp/`. `lsof -U` and
   `ls /tmp/*.{sock,pipe}` often reveal them.


## Don't

- Don't put driver code under `/usr/src/boot/`. That's kernel.
- Don't put user-editable config under `/etc/drivers/`. There
  isn't one. Driver config is the manifest at
  `/usr/lib/drivers/<name>/events.yaml`.
- Don't have the kernel interpret your payload. It routes by
  `kind` only; the receiving PAI parses the rest.
- Don't read `~/Library/Calendars/*.sqlitedb` directly when
  `EventKit` is available — Apple changes that schema without notice.
- **Don't write a synchronous `def run()`.** The kernel calls it to
  *get* a coroutine — anything but `async def` wedges boot. See
  "Driver runtime contract" above.
- **Don't `time.sleep` or `requests.post`/etc. inline.** Use
  `await asyncio.sleep` and `await asyncio.to_thread(blocking_call)`.
  A 30-second `requests` timeout in an async driver freezes every
  other driver and every PAI nudge for 30 seconds.
- **Don't import `subprocess.Popen` for long-running children.**
  Use `asyncio.create_subprocess_exec` so reading the child's stdout
  doesn't block the kernel.
- **Don't backfill history on first run.** Cursor at "now"; emit only
  what arrives after. Backfilling years of calendar/email/messages
  events floods every consumer PAI and produces zero value. See
  "Don't backfill history. Start from 'now.'" above.
- **Don't emit one event per item.** One real-world tick = one
  event with a list payload, not N events. If you could plausibly
  emit >10 events in <1s, you're fanning out where you should be
  batching. See "Event-volume budget — batch, don't fan out."
  above.
- **Don't poll when a notification API exists.** PAI is event-driven;
  a `while True: await asyncio.sleep(2); check()` loop against a file,
  DB, or app is almost always wrong. Use `watchdog` / FSEvents,
  `NSDistributedNotificationCenter`, IMAP IDLE, the bridge's stdout
  stream, or whatever push channel the surface offers.
- **Don't put non-Python code in `/usr/lib/drivers/<name>/`.** Sidecar
  sources (Node bridges, Rust binaries) belong in `/usr/libexec/<name>/`.
  Don't commit `node_modules/` / `target/` / `vendor/` in pairegistry —
  paiman populates them at install time. Don't hard-code
  `/opt/homebrew/bin/node` — resolve via `shutil.which`.
- **Don't ship test files (`*_test.py`, `test_*.py`) in
  `/usr/lib/drivers/<name>/`.** Tests live alongside source in
  pairegistry; the runtime bundle is production code only.

## Read these next

- `~/Projects/pairegistry/drivers/imessage/` — reference implementation.
- `/usr/src/boot/main.py` — `_discover_driver_specs`, `_handle_event_file`,
  `_route_to_pids`.
- `memory/doc/KERNEL_EVENTS.md` — how `kind` becomes a nudge.
- `memory/doc/FILESYSTEM_v3.md` — the three-location driver split.
- `memory/doc/PAIMAN.md` — install internals.
- `memory/doc/SELF_HEALING.md` — patching a driver in-place.
- Skill `kernel-restart` — restart procedure and caveats.
- Skill `restart-driver` — bounce a single driver without full reboot.
