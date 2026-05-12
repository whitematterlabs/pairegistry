---
name: grow-capability
visible_to: [root]
description: Classify a build request (own initiative or a child PAI's `request-capability:` send_message), pick the right bundle kind, hand off to claudecode, and notify the requester. Single source of truth for the bin / driver / skill / subagent / pai-bundle / prompt taxonomy.
---

# Growing a capability

Two entry points:

1. **A child PAI messaged you `request-capability: <need>` / `why: <ask>`.** Save the sender pid; you `send-message` them back at the end. Don't message the owner; don't invoke the new capability yourself. If the request is unintelligible, `send-message` the requester for refinement — never the owner.
2. **You decided to build something.** Skip the notify step at the bottom.

This skill is **Step 2** of root's build flow. Root's prompt already specifies the claudecode handoff, the ≤80-word brief, and the post-build help page — don't re-derive any of that here. Use it verbatim.

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

### Collapsibility test

> Can the request be served by an existing primitive under `/usr/lib/drivers/` plus a `bin` (or a `skill`), without losing native event hooks or piling per-call ceremony at the frequency this surface will actually be hit?
> Yes → **bin** (or skill). Always.

### Pick a kind

| Kind | When |
|---|---|
| `bin` | "Run X / fetch Y / book Z / format / post / drive a checkout / call an API." Returns a value via stdout + exit code. May be long-running, use credentials, drive a headless browser owned by an existing driver. **The default.** |
| `skill` | Procedural knowledge a PAI loads into its turn — a checklist, a triage flow, an authoring guide. No CLI. Read by `cat memory/skills/<name>/SKILL.md`. |
| `prompt` | A new role/system prompt — only when adding or replacing a PAI's identity. |
| `lib` | Importable Python shared by ≥2 bins/drivers. Don't reach for this until the duplication actually exists. |
| `driver` | A new **primitive surface**: app ABI (Mail, Messages), system framework (AddressBook), I/O channel (audio), or a shared long-lived session (headless browser). Two conditions, **both** required: (a) genuinely primitive, not a task; (b) collapsing into an existing driver would lose native event hooks or cost too much ceremony. See `memory/doc/KERNEL.md` for the driver contract. |
| `subagent` | A reusable research/specialist role spawned ephemerally by other PAIs via `bin/subagent spawn --package <name>` (e.g. `scout`). See `memory/doc/SUBAGENT_BUNDLES.md`. Rare — usually only when a new specialist persona is needed. Note: coding handoffs go through skill `execute-claudecode`, not a subagent bundle. |
| `pai` | A dedicated fleet member with its own identity, prompt, and event subscriptions. Pair with a driver when long-horizon turn-taking on that surface is wanted ("a calendar PAI", "an autonomous scheduler"). |
| `sbin` | Owner/root-only fleet-mutation tool. Almost never the answer to a capability request. |

### Two failure modes

- **Splitter** — promoting a *task* ("reservations driver", "ordering driver") into a driver when it collapses to an existing primitive + a bin. Almost always wrong.
- **Lumper** — collapsing a high-frequency reactive surface (mail, messages) into a generic bin when doing so loses native event hooks. Wrong when both frequency and reactivity are high.

If the brief reads "book / send / post / buy / search / run / fetch" — that's a **bin**. Stop.

## Step 3 — fire claudecode

Use skill `execute-claudecode` to fire the brief. The brief shape (≤80 words, type/name/need/why/shape) is specified in root's prompt and in `execute-claudecode`; don't re-derive. Two kind-specific notes that don't belong in the prompt:

- **driver**: before firing, settle five questions and put them in the brief: top-level dir, partition key, one entity-file shape, event kinds (`<surface>:new|changed|removed`), external source (sqlite path / API / AX). After claudecode lands it, `paiman install` → `paictl start <name>-in` → `reboot`. Background: `memory/doc/KERNEL.md`.
- **pai bundle**: do the driver first if one is missing. Bundle brief must name `wake_on:` globs and a one-sentence role. After it lands, `paiadd <bundle>`.

For `bin` / `skill` / `prompt` / `lib` / `subagent`: just the standard brief. No pre-design.

## The shape contract

`shape:` in the brief is *what proves it works*, not *how it's built*.

- bin: one CLI invocation line that, when run, demonstrates the tool. `cal --today` not "uses EventKit".
- skill: one-line acceptance criterion ("a PAI reading this can author a driver end-to-end").
- driver: the event-kind line the coder must emit on the first real change.

If the line names a library, a path under `/usr/lib/`, async vs sync, or an error-handling rule — delete it. That's HOW.

## Step 4 — verify, then notify

Spot-check the artifact yourself (run the `shape:` line, `ls` the new files) before sending the reply. Claudecode's own verify isn't enough — re-run it.

```sh
bin/send-message --to <requester pid> --content 'capability-ready: <name> — usage: <cli or one-line>'
# or
bin/send-message --to <requester pid> --content 'capability-failed: <name> — reason: <one line>'
```

New `bin/` tools appear in the requester's next turn automatically. New drivers and PAIs appear in `/proc/` after `kernel:reload_config` (handled by `reboot`).

## Boundaries

- Build and notify. Don't invoke the new capability yourself.
- Don't message the owner. The requester handles user-facing follow-through.
- For refinement, ask the **requester**, never the owner.
- Data is a file, tools are binaries, long-horizon work is a new PAI.

## Build event-driven, always

PAI reacts to events. Every reactive capability resolves to one of:

- **Existing driver covers the surface** — wire `wake_on:` to its
  `<surface>:new|changed|removed`.
- **No driver covers it** — build a driver that observes natively
  (file watch, sqlite trigger, webhook, OS callback) and emits
  events.
- **Clock-bound trigger** ("remind me at 3pm") — `paicron`
  schedules a one-shot event.

A `shape:` line that says "every N seconds," "periodically," or
"checks for new X" means the design is wrong — usually a missing
driver. Rescope before firing claudecode.

## Logging

One line to `/proc/root/log.md` per phase: received, scoped (kind chosen), spawned, delivered (or failed).
