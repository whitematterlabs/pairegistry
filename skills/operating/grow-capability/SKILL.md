---
name: grow-capability
description: Handle a `request-capability:` IPC from a child PAI by scoping the need, choosing the right build path (bin tool / driver / PAI bundle), executing it, and messaging the requester back. The "growth" half of the capability-gap escalation ceremony.
---

# Growing a capability for a requester

A child PAI just messaged you with `request-capability: ...` because
its owner asked for something it has no tool for. Your job: scope the
need, build the right thing, then notify the requester. You do **not**
run the new capability yourself, and you do **not** message the owner —
the requester owns the user-facing follow-through.

## Inputs

The IPC envelope gives you:
- `from: pai:<requester pid>` — the PAI that asked. Save this; you
  IPC them back at the end.
- `content:` — two lines:
  - `request-capability: <need>`
  - `why: <owner's ask>`

The requester does **not** classify scope, shape, or surface — that is
your job here. You infer the build path from `why:` plus a sanity
check against what's already installed.

If a field is missing or unintelligible, IPC the requester back asking
them to refine the request. Don't guess at intent.

## Step 1 — sanity check

Before building anything, look for an existing answer:

```sh
ls bin/                          # is there already a tool that does this?
ls memory/skills/                # is there a skill that covers it?
ls /usr/lib/skills/              # ditto for system skills
ls /usr/lib/drivers/             # is there a driver that already surfaces this data?
```

If yes, just IPC the requester pointing at the existing tool/skill:

```sh
bin/send-message --to <requester pid> --content 'capability-exists: <name> — usage: <how to call>'
```

Done. Log a one-liner to `/proc/root/log.md` and return.

## Step 2 — scope triage

**Drivers are primitives, not tasks.** Look at what's already
installed under `/usr/lib/drivers/`: every driver is a *surface* —
an app ABI (Mail.app, Messages), a system framework (AddressBook),
an I/O channel (audio), or a long-lived session primitive. None of
them are jobs-to-be-done like "scheduling", "ordering", or
"reservations". Drivers exist because a primitive surface earns
filesystem mediation; tasks ride on top of primitives as bins.

Apply the **collapsibility test** before considering any new driver:

> Can the request be served by an existing primitive driver
> (`ls /usr/lib/drivers/`) plus a bin or skill, without losing
> native event-watching or imposing per-call ceremony at the
> frequency this surface will actually be hit?
> If yes → Scope A. Always.

Two failure modes:
- **Splitter** — promoting a task ("reservations driver", "ordering
  driver", "X-app driver" for a one-off) into a new driver when it
  collapses cleanly into an existing primitive + a bin. Almost
  always wrong.
- **Lumper** — collapsing a high-frequency reactive surface
  (mail, messages) into a generic primitive when doing so would
  lose native event hooks or pile ceremony on every read/write.
  Wrong when both frequency and reactivity are high.

If you catch yourself building a driver for "book a reservation",
"send a message via app X", "post a status", "buy this thing", "run
a search" — **stop**. Those are bins on top of an existing primitive
(usually browser, sometimes osascript or an app ABI driver that
already exists). A new driver only happens when you've identified a
*primitive surface* the fleet doesn't yet have.

### Scope A — bin tool

A CLI invocation that returns a value. The PAI-facing contract is
`bin/foo --args` → stdout/exit code. No spool, no fleet-wide on-disk
shape, no follow-up events.

Signals:
- "book a reservation", "post a tweet", "fetch a URL", "format a
  date", "play a tone", "drive a checkout flow", "run an osascript"
- May be long-running, may use credentials, may drive a headless
  browser session owned by an existing primitive driver, may spend
  money. Still a bin.

→ Go to **Step 3A**.

### Scope B — driver

A new *primitive surface*. The PAI-facing contract is *files on
disk* in a spool the driver owns; the driver keeps those files in
sync with the external world (both directions where applicable) and
emits `kind:` events on lifecycle changes.

Earns its own driver only when **both** are true:
- It is a real primitive (app ABI, system framework, I/O channel,
  shared long-lived session like a headless browser).
- Collapsing it into an existing primitive driver would cost native
  event hooks or impose unacceptable ceremony at its real-world
  frequency.

Examples that earned drivers: Mail.app (drafts/sent/INBOX symmetry,
high frequency, reactive), Messages (SQLite + native hooks), a
shared headless browser session.

Examples that do **not** earn drivers: reservations, ordering,
weather, search, "X-app integration" for a one-off task — those are
bins on top of an existing primitive.

→ Go to **Step 3B**.

### Scope C — driver + PAI bundle

Scope B *and* the request warrants a dedicated fleet member with its
own identity, prompt, and long-horizon turn-taking on those events.

Signals:
- "I need a calendar PAI", "add an autonomous scheduler", "something
  that manages X on its own"

→ Go to **Step 3C** (driver first, then PAI bundle).

---

## Step 3A — build a bin tool

Spawn the `claudecode` subagent with the brief:

```sh
bin/subagent spawn --slug claudecode-<topic> --package claudecode --prompt "
type: bin
name: <name>
need: <verbatim from request>
why: <verbatim why>
"
```

Wait for `subagent:response` / `proc completed`, then go to **Step 4**.

---

## Step 3B — build a driver

Pull the authoring skill first:

```sh
cat memory/skills/author-driver/SKILL.md
```

**Before designing anything**, research how to reach the external surface:

- Web search `<surface> macOS API python` — look for prior art.
- If no data API exists, check the Accessibility API (`AXUIElement`),
  `ScriptingBridge`, `NSDistributedNotificationCenter`, or the app's
  own XPC/socket IPC. See the `author-driver` skill for a full list.
- If still uncertain, run a quick spike (10-line script) to confirm
  the approach works before writing the full driver.

Then design the filesystem layout before writing any code (see
`pai-dogma` §"Canonical filesystem layout"). Answer these before
building:

1. What is the top-level directory? (e.g. `/calendar/`, `/contacts/`)
2. What is the partition key? (date, entity slug, etc.)
3. What does one entity file look like? (flat YAML fields)
4. What events does the driver emit? (`<surface>:new`, `<surface>:changed`, `<surface>:removed`)
5. Is there an existing external DB/API to read? (e.g. SQLite at `~/Library/Calendars/`)

Spawn the `claudecode` subagent with a **driver brief**:

```sh
bin/subagent spawn --slug claudecode-<topic> --package claudecode --prompt "
type: driver
name: <name>
need: <what the driver must do>
why: <owner's ask>
filesystem_layout: |
  <surface>/
    <partition>/
      <entity>.yaml
external_source: <path or API, e.g. ~/Library/Calendars/*.sqlitedb>
events:
  - kind: <surface>:new
  - kind: <surface>:changed
  - kind: <surface>:removed
no_polling: true  # use FSEvents / SQLite WAL hooks, not sleep loops
"
```

After the driver is built, install and activate it:

```sh
sbin/paiman install /usr/lib/drivers/<name>/
bin/paictl start <name>-in
bin/paictl restart
```

Then go to **Step 4**.

---

## Step 3C — build a driver + PAI bundle

Do **Step 3B** first (driver). Then pull the bundle authoring skill:

```sh
cat memory/skills/author-pai-bundle/SKILL.md
```

Design the bundle:
1. What events does this PAI wake on? (the new driver's `kind:` globs)
2. What is its role in one sentence? (this becomes the bundle's prompt)
3. Does it need any `bin/` tools beyond what exists?

Spawn claudecode for the bundle:

```sh
bin/subagent spawn --slug claudecode-<bundle-topic> --package claudecode --prompt "
type: pai-bundle
name: <name>-pai
need: A PAI that handles <surface> operations for the owner.
wake_on:
  - <surface>:*
prompt_summary: <one sentence role description>
required_drivers:
  - <driver-name>
"
```

After claudecode finishes, instantiate the new PAI:

```sh
sbin/paiadd <bundle-name>
```

Then go to **Step 4**.

---

## Step 4 — verify and notify the requester

Read the result files to confirm success:

```sh
cat proc/claudecode-<topic>/result.md
cat proc/claudecode-<topic>/log.md
```

On success:

```sh
bin/send-message --to <requester pid> --content 'capability-ready: <name> — usage: <cli or description>'
```

On failure:

```sh
bin/send-message --to <requester pid> --content 'capability-failed: <name> — reason: <one line>'
```

The requester's next turn will see any new `bin/` tools automatically
(the kernel re-lists `bin/` every turn). New drivers and PAIs appear
in `/proc/` after `kernel:reload_config`.

## Boundaries

- Build and notify. Do **not** invoke the new capability yourself.
- Do **not** message the owner. The requester does that.
- Do **not** ask the owner for clarification — work from the
  requester's `need:` and `why:`. If those are too thin, IPC the
  requester (not the owner) for a refined request.
- Don't over-engineer. Keep the FHS contract: **data is a file**
  (flat YAML under `/calendar/`, `/contacts/`, etc.), **tools are
  binaries** (one-shot CLIs under `/bin/`), and **long-horizon work
  is a new PAI instance** (scope C, not a polling loop in a bin tool).
  No polling, filesystem-first, simplest thing that works.

## Logging

One line to `/proc/root/log.md` per phase: request received, scope
decided, build spawned, capability delivered (or failed).
