---
name: understand-ipc
description: How PAIs talk to each other — pai_message and subagent:response, the bin/send-message and bin/subagent CLIs.
---

# Inter-PAI IPC

PAIs don't call each other synchronously. Every cross-PAI exchange
goes through the **event bus** as a directed event with a
`target_pid:` field — no `wake_on` glob fan-out, point-to-point.

## Two directed kinds

| Kind | Direction | Emitter | Use |
|---|---|---|---|
| `pai_message` | any → any | `bin/send-message --to <pid> --content "..."` | generic peer IPC |
| `subagent:response` | child → parent | `bin/subagent reply --content "..."` | child reporting back |

Spawn kickoff prompts ride `pai_message` — the parent's first IPC
to a newborn child is just a regular message.

The receiving PAI gets the event in its user turn. For
`subagent:response`, the parent additionally sees `reason: subagent
response` so it knows at a glance the message is from one of its
own children.

## CLIs

```sh
# Send a message to PAI at pid 2
bin/send-message --to 2 --content "fyi: gmail driver restarted"

# Addressing by slug also works
bin/send-message --to research-flights-2026-07-07 --content "budget is $500"

# Emit a kernel event (no target_pid; broadcast through wake_on)
bin/paictl reload

# Spawn a subagent (one task, then done)
bin/subagent spawn --slug research-flights \
    --prompt "find me flights to istanbul"

# Child reports back (uses $PAI_PARENT to know where to send)
bin/subagent reply --content "found: THY 1234 at $452"

# End a subagent
bin/subagent kill --slug research-flights
```

Subagents live for one task: the kickoff `--prompt` arrives as a
`pai_message`, the child works, replies, and is reaped. Spec marker
is `persistent: true` (stay alive across turns until killed); system
prompt fragment is `usr/share/prompts/subagent.md`.

## Why this matters

A parent can drive N concurrent children without blocking — every
turn is mediated by the bus, not by a synchronous call. The kernel
nudges the parent on each child reply.

## Read these next

- `memory/doc/KERNEL.md` §"Inter-PAI messaging"
- `/usr/src/bin/subagent.py` — the subagent CLI.
- Skill `understand-event-routing` — directed vs broadcast events.
