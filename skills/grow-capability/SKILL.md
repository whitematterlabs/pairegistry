---
name: grow-capability
description: Handle a `request-capability:` IPC from a child PAI by scoping the need, spawning the coder subagent to build the smallest tool that satisfies it, and messaging the requester back. The "growth" half of the capability-gap escalation ceremony.
---

# Growing a capability for a requester

A child PAI just messaged you with `request-capability: ...` because
its owner asked for something it has no tool for. Your job: turn
that need into a working `bin/` tool the requester can use on its
next turn, then notify the requester. You do **not** run the tool
yourself, and you do **not** message the owner — the requester owns
the user-facing follow-through.

## Inputs

The IPC envelope gives you:
- `from: pai:<requester pid>` — the PAI that asked. Save this; you
  IPC them back at the end.
- `content:` — three lines:
  - `request-capability: <need>`
  - `why: <owner's ask>`
  - `shape: <desired CLI>`

If a field is missing or unintelligible, IPC the requester back asking
them to refine the request. Don't guess at intent.

## Step 1 — sanity check

Before building anything, look for an existing answer:

```sh
ls bin/                          # is there already a tool that does this?
ls memory/skills/                # is there a skill that covers it?
ls /usr/lib/skills/              # ditto for system skills
```

If yes, just IPC the requester pointing at the existing tool/skill:

```sh
bin/ipc --to <requester pid> --content 'capability-exists: <name> — usage: <how to call>'
```

Done. Log a one-liner to `/proc/root/log.md` and return.

## Step 2 — spawn the coder subagent

If genuinely new, hand the brief to the `coder` subagent bundle. Its
role prompt knows the default posture (smallest thing that works,
write to the correct location for the type, verify, leave a
`result.md`, call `subagent done`). You only supply the brief:

```sh
bin/subagent spawn --slug coder-<topic> --package coder --prompt "
type: bin
name: <name>
need: <verbatim from request>
why: <verbatim why>
shape: <verbatim shape>
"
```

That's it. No further instructions to the coder needed — the bundle
supplies its own role.

## Step 3 — wait for the subagent

Return your turn. The kernel will nudge you with `subagent response`
or `proc completed` when the coder finishes. That's a normal nudge —
no special handling needed here, just resume on the next turn.

## Step 4 — notify the requester

When the coder is done, read `proc/coder-<topic>/result.md` and
`proc/coder-<topic>/log.md` to confirm the tool works.

On success:

```sh
bin/ipc --to <requester pid> --content 'capability-ready: <name> — usage: <cli>'
```

The requester's next turn will list `bin/<name>` in its `<bin>`
block automatically — the kernel re-lists `bin/` every turn, so no
registry update is needed.

On failure:

```sh
bin/ipc --to <requester pid> --content 'capability-failed: <name> — reason: <one line>'
```

So the requester can tell the owner honestly.

## Boundaries

- You build and notify. You do **not** invoke the new tool.
- You do **not** message the owner. The requester does that.
- Do **not** ask the owner for clarification — work from the
  requester's `why:` and `shape:`. If those are too thin, IPC the
  requester (not the owner) for a refined request.
- Don't over-engineer. A 20-line shell script that satisfies the
  shape beats a configurable framework.

## Logging

One line to `/proc/root/log.md` per phase: request received, coder
spawned, capability delivered (or failed). That's the audit trail.
