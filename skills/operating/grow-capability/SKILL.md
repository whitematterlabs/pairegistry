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
- `content:` — three lines:
  - `request-capability: <need>`
  - `why: <owner's ask>`
  - `shape: <desired CLI or description>`

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
bin/nudge --to <requester pid> --content 'capability-exists: <name> — usage: <how to call>'
```

Done. Log a one-liner to `/proc/root/log.md` and return.

## Step 2 — scope triage

Classify the request into one of three scopes:

### Scope A — bin tool

A one-shot CLI utility. No persistent process. No external data source.

Signals:
- "I need to play a tone", "I need to fetch a URL", "I need to format a date"
- The shape is a single command invocation
- No ongoing sync with an external system

→ Go to **Step 3A**.

### Scope B — driver

The request implies reading from or writing to an **external data
source** that changes over time (Calendar.app, Contacts, GitHub,
Spotify, a database). PAI needs to react to those changes.

Signals:
- "I need calendar access", "I need contacts data", "sync X into PAI"
- The data lives outside PAI and has its own schema
- Multiple PAIs might want to consume the same data

→ Go to **Step 3B**.

### Scope C — driver + PAI bundle

The request implies a new **fleet member** with its own identity: it
needs dedicated reasoning, its own prompt, and wakes on the new
driver's events.

Signals:
- "I need a calendar PAI", "add a scheduler", "I need something to
  manage X autonomously"
- The capability is broad enough that it warrants its own turn-taking
  identity separate from the requesting PAI

→ Go to **Step 3C** (driver first, then PAI bundle).

---

## Step 3A — build a bin tool

Spawn the `coder` subagent with the brief:

```sh
bin/subagent spawn --slug coder-<topic> --package coder --prompt "
type: bin
name: <name>
need: <verbatim from request>
why: <verbatim why>
shape: <verbatim shape>
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

Spawn the `coder` subagent with a **driver brief**:

```sh
bin/subagent spawn --slug coder-<topic> --package coder --prompt "
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
bin/nudge emit kernel:restart
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

Spawn coder for the bundle:

```sh
bin/subagent spawn --slug coder-<bundle-topic> --package coder --prompt "
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

After coder finishes, instantiate the new PAI:

```sh
sbin/paiadd <bundle-name>
```

Then go to **Step 4**.

---

## Step 4 — verify and notify the requester

Read the result files to confirm success:

```sh
cat proc/coder-<topic>/result.md
cat proc/coder-<topic>/log.md
```

On success:

```sh
bin/nudge --to <requester pid> --content 'capability-ready: <name> — usage: <cli or description>'
```

On failure:

```sh
bin/nudge --to <requester pid> --content 'capability-failed: <name> — reason: <one line>'
```

The requester's next turn will see any new `bin/` tools automatically
(the kernel re-lists `bin/` every turn). New drivers and PAIs appear
in `/proc/` after `kernel:reload_config`.

## Boundaries

- Build and notify. Do **not** invoke the new capability yourself.
- Do **not** message the owner. The requester does that.
- Do **not** ask the owner for clarification — work from the
  requester's `why:` and `shape:`. If those are too thin, IPC the
  requester (not the owner) for a refined request.
- Don't over-engineer. Keep the FHS contract: **data is a file**
  (flat YAML under `/calendar/`, `/contacts/`, etc.), **tools are
  binaries** (one-shot CLIs under `/bin/`), and **long-horizon work
  is a new PAI instance** (scope C, not a polling loop in a bin tool).
  No polling, filesystem-first, simplest thing that works.

## Logging

One line to `/proc/root/log.md` per phase: request received, scope
decided, build spawned, capability delivered (or failed).
