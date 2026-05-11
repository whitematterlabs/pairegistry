---
name: kernel-tools
visible_to: [root]
description: Cheatsheet for paiman, paiadd/paidel, paictl, paicron, send-message, subagent — what each does, when to use which. Read before invoking unfamiliar tooling.
---

# Kernel tooling

Five command families, each scoped to one concern. Don't reach for
a different one to "make it work."

## paiman — bundle layer

Manages templates (`/opt/<pkg>/<ver>/`, `/usr/lib/pais/<name>/`).

These are **sbin tools** — available in root's home as `sbin/paiman`
etc., or by absolute FHS path `/sbin/paiman`. Non-root PAIs don't
have `sbin/` in their home.

```sh
sbin/paiman init <name>       # scaffold a new dev bundle at /usr/lib/pais/<name>/
sbin/paiman install <bundle>  # install a release bundle into /opt/
sbin/paiman uninstall <bundle># refused if any instance references it
sbin/paiman list              # available bundles
```

## paiadd / paidel — instance layer

Configure / unconfigure a PAI. Wizard-style; writes
`/etc/config.yaml` and `/var/lib/instances/<name>/`.

```sh
sbin/paiadd <bundle>               # useradd-style wizard
sbin/paidel <name>                 # remove fleet entry; preserves instance state (sacred)
sbin/paidel <name> --purge         # also wipe /var/lib/instances/<name>/
```

Both end by emitting `kernel:reload_config`. **This is the supported
path for adding/removing a PAI** — hand-editing `/etc/config.yaml`
to add or remove is wrong.

## paictl — instance runtime

Start/stop fleet members already configured.

```sh
paictl start <name>           # flip /proc/<name>/spec.yaml `active: true`
paictl stop <name>             # flip to false
paictl status                 # one-line per PAI
```

Both flip the `active:` flag and emit `kernel:reload_config`.
Reconcile is event-driven, never polled.

## paicron — service layer

Spawn cron jobs, watchers, and async work — the systemctl-shaped
frontend for `/proc/<slug>/`.

```sh
# Background subagent (one-shot)
paicron start --slug research-flights \
    --run "bin/subagent 'flights to istanbul'" \
    --restart never

# Cron job (recurring)
paicron start --slug nightly-consolidation \
    --schedule "0 3 * * *" \
    --run "bin/consolidate"

# Reminder (no run:, just a timer that nudges PAI on fire)
paicron start --slug call-mom \
    --schedule "2026-05-04T18:00:00"

paicron stop <slug>           # mark cancelled
paicron list                  # one-line per /proc/ entry
```

`paicron` auto-suffixes the slug with `-YYYY-MM-DD` (or full
timestamp on same-day collision).

## send-message — peer messaging

```sh
# Send a directed message to another PAI
send-message --to <pid|slug> --content "..."
```

## reboot

To pick up `/etc/config.yaml` hand-edits, run `sbin/reboot`. There is
no separate reload CLI — reconcile runs at every boot.

## subagent — child PAI lifecycle

```sh
# Ephemeral (one-task)
subagent spawn --slug <name> --prompt "..."
subagent reply --content "..."        # from inside the child
subagent kill --slug <name>           # end an ephemeral child

# Persistent (persub) — see memory/doc/PERSUBS.md
subagent spawn --persistent --slug <name> [--prompt "..."]
```

`subagent kill` is **rejected** for persubs.

## Standard flow — bringing a new capability online

The owner asks "set up email" / "add calendar" / "wire up messages."
Four tools, in order. Don't skip steps; don't try to grep kernel source
for the capability — `paiman` is the package manager, ask it.

```sh
# 1. Discover. paiman is the package manager — ask it.
sbin/paiman search                    # everything available
sbin/paiman search email              # filter by name
sbin/paiman search --kind pai         # filter by bundle kind (driver|skill|pai|bin|prompt)

# 2. Install the bundle. Pulls deps (drivers, skills, bins) automatically.
sbin/paiman install email-pai

# 3. Configure an instance. Wizard writes /etc/config.yaml +
#    /var/lib/instances/<name>/. Emits kernel:reload_config.
sbin/paiadd email-pai                  # → instance, e.g. "email"

# 4. Mark the instance active so the supervisor spawns it.
bin/paictl start email

# 5. Re-exec the kernel so new driver wake_on globs land in the router.
sbin/reboot
```

`paiadd` and `paictl` configure and run; they don't install. `paiman` is
the only thing that touches `/opt/paiman/` and the activation slots.

## When to use which

| Situation | Tool |
|---|---|
| Find what's installable (email, calendar, …) | `sbin/paiman search [pattern]` |
| Install a release bundle | `sbin/paiman install <name>` |
| Add a new PAI to the fleet | `sbin/paiadd <bundle>` |
| Start a configured-but-stopped PAI | `bin/paictl start <name>` |
| Stop running a PAI temporarily | `bin/paictl stop` |
| Schedule a one-shot reminder | `bin/paicron start --schedule …` |
| Wake the kernel after editing `/etc/config.yaml` | `sbin/reboot` |
| Pick up new driver `wake_on:` globs | `sbin/reboot` |
| Send a message to another PAI | `bin/send-message --to …` |
| Spawn a research subagent | `bin/subagent spawn` |

## Read these next

- `memory/doc/KERNEL.md` — kernel internals, including spawning and reconcile.
- `memory/doc/FILESYSTEM_v3.md` — proc/ layout that paicron and paictl write to.
