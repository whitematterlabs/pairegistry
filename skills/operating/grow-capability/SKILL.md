---
name: grow-capability
visible_to: [root]
description: Classify a capability request (own initiative or a child PAI's `request-capability:` send_message), pick the right bundle kind, install existing registry bundles when available, and notify the requester. Single source of truth for the bin / driver / skill / subagent / pai / prompt taxonomy.
---

# Growing a capability

Two entry points:

1. **A child PAI messaged you `request-capability: <need>` / `why: <ask>`.** Save the sender pid; you `send-message` them back at the end. Don't message the owner; don't invoke the new capability yourself. If the request is unintelligible, `send-message` the requester for refinement — never the owner.
2. **You decided a capability is missing.** Skip the notify step at the bottom.

This skill is root's capability-gap flow. It does not invoke Claude Code or any other code-generation subprocess. Root can discover and install existing registry bundles; source authoring belongs in the canonical repositories outside the runtime.

## Step 1 — does it already exist?

```sh
ls bin/                          # owner-callable tools
ls memory/skills/                # this PAI's skills
ls /usr/lib/skills/              # all installed skills
ls /usr/lib/drivers/             # primitive surfaces
paiman search <keyword>          # available to install but not yet
```

Hit → `send-message --to <requester pid> --content 'capability-exists: <name> — usage: <how to call>'` and stop.

## Step 2 — scope triage (single source of truth)

Installable bundle kinds (from `paiman.py`): **bin, sbin, driver, skill, prompt, lib, subagent, pai**.

**Default to `bin` when unsure.** Bins are cheap: one file, one CLI contract, no lifecycle. Drivers are expensive: FHS layout under `/usr/lib/drivers/<name>/`, `events.yaml` vocabulary, supervisor lifecycle, on-disk spool in sync with an external world. Don't pay that cost unless the surface earns it.

### Gate 1 — reactive or imperative?

Answer this first. A reactive capability ("notify me when X changes", "remind me before Y", "wake on Z") needs a driver emitting events. An imperative capability ("run X", "fetch Y", "post Z") doesn't.

- **Reactive + driver already exists** (check `ls /usr/lib/drivers/` and its `events.yaml`) → wire `wake_on:` to its events. No new bundle needed, or a thin skill.
- **Reactive + the surface is a web page or HTTP/RSS feed with no driver** ("watch this listing / stock / page, tell me when it changes") → follow skill `setup-watcher`. That is the sanctioned cheap-poll path: a cron-fired subprocess that diffs the page and wakes the requester *only* when a condition fires. This is **not** "a bin that polls" below — it adds no hot loop and never wakes anyone on a quiet tick.
- **Reactive + a genuine new primitive surface** (app ABI, system framework, push-capable channel) and no driver → the gap requires a driver bundle/source change. Stop. Don't install a bin that polls a primitive — that's the wrong scope, not a faster path.
- **Imperative** → continue to Gate 2.

An existing **bin** for the surface does not count — bins don't emit events. Only a driver, or the `setup-watcher` cheap-poll path for web pages/feeds, does.

### Gate 2 — bin or something heavier?

For imperative work, default to `bin`. Pick from the table below only if `bin` clearly doesn't fit.

### Pick a kind

| Kind | When |
|---|---|
| `bin` | "Run X / fetch Y / book Z / format / post / drive a checkout / call an API." Returns a value via stdout + exit code. May be long-running, use credentials, drive a headless browser owned by an existing driver. **The default.** |
| `skill` | Procedural knowledge a PAI loads into its turn — a checklist, a triage flow, an authoring guide. No CLI. Read by `cat memory/skills/<name>/SKILL.md`. |
| `prompt` | A new role/system prompt — only when adding or replacing a PAI's identity. |
| `lib` | Importable Python shared by ≥2 bins/drivers. Don't reach for this until the duplication actually exists. |
| `driver` | A new **primitive surface**: app ABI (Mail, Messages), system framework (AddressBook), I/O channel (audio), or a shared long-lived session (headless browser). Two conditions, **both** required: (a) genuinely primitive, not a task; (b) collapsing into an existing driver would lose native event hooks or cost too much ceremony. See `memory/doc/KERNEL.md` for the driver contract. |
| `subagent` | A reusable research/specialist role spawned ephemerally by other PAIs via `bin/subagent spawn --package <name>` (e.g. `scout`). See `memory/doc/SUBAGENT_BUNDLES.md`. Rare — usually only when a new specialist persona is needed. |
| `pai` | A dedicated fleet member with its own identity, prompt, and event subscriptions. Pair with a driver when long-horizon turn-taking on that surface is wanted ("a calendar PAI", "an autonomous scheduler"). |
| `sbin` | Owner/root-only fleet-mutation tool. Almost never the answer to a capability request. |

### Two failure modes

- **Splitter** — promoting a *task* ("reservations driver", "ordering driver") into a driver when it collapses to an existing primitive + a bin. Almost always wrong.
- **Lumper** — collapsing a high-frequency reactive surface (mail, messages) into a generic bin when doing so loses native event hooks. Wrong when both frequency and reactivity are high.

If the request reads "book / send / post / buy / search / run / fetch" — that's a **bin**. Stop.

## Step 3 — install or report the gap

Search the registry for an existing bundle before declaring new source work is needed:

```sh
paiman search <keyword>
paiman show <candidate>
```

If an existing bundle matches the need:

- `bin` / `skill` / `prompt` / `lib` / `subagent`: `paiman install <candidate>`.
- `driver`: `paiman install <candidate>` → `paictl start <name>-in` if it has a runnable process → `reboot`.
- `pai`: follow skill `kernel-tools`: `paiman install <candidate>` → `paiadd <candidate>` → `paictl start <instance>` → `reboot`.

If no existing bundle matches, do **not** invoke Claude Code, create files in `/usr/lib/`, or hand-author source inside the runtime. Report that the capability requires a source change in `/Users/arda/Projects/pai` or `/Users/arda/Projects/pairegistry`.

## The acceptance shape

Use this to decide whether an existing bundle satisfies the request. It is *what proves it works*, not *how it is built*.

- bin: one CLI invocation line that, when run, demonstrates the tool. `cal --today` not "uses EventKit".
- skill: one-line acceptance criterion ("a PAI reading this can author a driver end-to-end").
- driver: the event-kind line the driver must emit on the first real change.

If the line names a library, a path under `/usr/lib/`, async vs sync, or an error-handling rule — delete it. That's HOW.

## Step 4 — verify, then notify

Spot-check the installed bundle yourself (run the acceptance line, `ls` the installed files, or check `/proc/` for drivers/PAIs) before sending the reply.

```sh
bin/send-message --to <requester pid> --content 'capability-ready: <name> — usage: <cli or one-line>'
# or
bin/send-message --to <requester pid> --content 'capability-needs-source-change: <name> — reason: <one line>'
# or
bin/send-message --to <requester pid> --content 'capability-failed: <name> — reason: <one line>'
```

New `bin/` tools appear in the requester's next turn automatically. New drivers and PAIs appear in `/proc/` after `kernel:reload_config` (handled by `reboot`).

## Boundaries

- Install or report the gap, then notify. Don't invoke the new capability yourself.
- Don't message the owner. The requester handles user-facing follow-through.
- For refinement, ask the **requester**, never the owner.
- Data is a file, tools are binaries, long-horizon work is a new PAI.

## Logging

One line to `/proc/root/log.md` per phase: received, scoped (kind chosen), installed or needs-source-change, delivered (or failed).
