---
name: inspect-fleet
description: Use to survey fleet state — which PAIs and drivers are configured, running, or failed. The reflex when an event is ambiguous or you need ground truth before acting. Root-only.
---

# Inspect the fleet

## First look

```
paictl ls                       # every PAI + driver with active + status
```

Columns: NAME / ACTIVE (config intent) / STATUS (runtime). Mismatch — `active=yes` but `status=failed` or missing — is the interesting case.

## Drill into one entry

```
paictl status <name>            # spec + active + runtime status
paictl logs <name>              # tail /proc/<name>/log.md (-f to follow)
paictl tokens <name>            # context usage rollup
```

Raw files behind those (when paictl is not enough):

- `/proc/<slug>/spec.yaml` — what reconcile gave it.
- `/proc/<slug>/status` — one of: `spawned`, `running`, `completed`, `expired`, `cancelled`, `failed`, `stopped`.
- `/proc/<slug>/log.md` — operational tail.

## PAI vs driver

- **PAI** (`root`, `pai`, …): declared in `/etc/config.yaml`. Sacred state at `/var/lib/instances/<name>/`; stitched home at `/root/` or `/home/<name>/`.
- **Driver** (`imessage-in`, `gmail-in`, …): no `/etc/` entry — `/proc/<slug>/spec.yaml` is the source of truth. Code at `/usr/lib/drivers/<pkg>/`; cursors at `/sys/drivers/<pkg>/`. Process slugs end in `-in`/`-out`; the package name omits that suffix.

## Source-of-truth checks

- `/etc/config.yaml` declares the PAI fleet. If `/proc/` and config disagree, reconcile is stale → emit `kernel:reload_config`.
- `/usr/lib/drivers/*/events.yaml` is the routing vocabulary. Cross-reference with `wake_on:` patterns in config.

## Common questions

- *Who handles event X?* — grep `wake_on:` in `/etc/config.yaml` for a glob over X's `kind`. Zero matches → entries with `fallback: true`; still zero → root.
- *Which driver emits this kind?* — `grep -rn "kind:" /usr/lib/drivers/*/events.yaml | grep <kind>`.
- *Why is this PAI stuck?* — `paictl status <name>` + `paictl logs <name>`. Terminal status with `active=yes` means it crashed and was not respawned; nudge or `paictl stop && paictl start`.
- *Going deeper?* — `memory/doc/KERNEL.md` (lifecycle, reconcile), `memory/doc/FILESYSTEM_v3.md` (layout), `memory/doc/PERSUBS.md` (persistent subagents).
