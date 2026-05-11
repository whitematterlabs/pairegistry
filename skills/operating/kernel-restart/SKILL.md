---
name: kernel-restart
description: Root-only. Re-exec the kernel in place via `sbin/reboot`. Covers when to reboot vs reload, and what survives the exec.
---

# Kernel restart

`sbin/reboot` emits `kernel:restart`. PID 1 drains in-flight nudges
(awaits every `_pai_locks` entry), runs the normal shutdown finally
(cancels driver tasks, resolves non-cron procs, kills tmux sockets,
reaps the pgrp), then `entry.py` `os.execvp`s the same argv. PID is
preserved.

**Survives the exec:** `/proc/<slug>/spec.yaml` (incl. `active:`
flags), `/sys/drivers/*` cursors, `/etc/config.yaml`, cron procs with
a `schedule:` (preserved deliberately so `rebuild_from_proc` re-arms
them), the kernel pid.

**Lost:** in-flight nudge turns past the drain point, ephemeral
tmux servers, any non-cron `status: running` proc (resolved to
`stopped` on the way out — reconcile restarts them if `active: true`).

## Reboot when

- New driver installed (`paiman install <name>`) — `events.yaml` is
  only walked at boot by `_discover_driver_specs`. New
  `processes:`/`wake_on:` globs aren't visible until re-exec.
- Kernel source changed (anything under `src/boot/`, or a privileged
  wrapper in `src/sbin/`/`src/bin/` that the kernel imports).
- `/etc/config.yaml` hand-edited in a way `kernel:reload_config`
  can't pick up (rare — fleet/prompt/model edits reload fine).

## Do NOT reboot for

- Fleet mutations (`paiadd`, `paidel`) — they emit
  `kernel:reload_config`.
- Flipping a driver/PAI on or off — `paictl start|stop <slug>` writes
  `active:` and emits reload.
- Cron edits — `paicron` handles its own re-arm.

If `paictl reload` (or the equivalent event) covers it, use that.

## Procedure

1. Coordinate. Any PAI mid-turn holds a `_pai_locks` entry; restart
   blocks until those release. Nudges that arrive during the drain
   queue against the lock and run post-reboot.

   ```sh
   ls /proc/*/status 2>/dev/null | xargs grep -l '^running' || echo "fleet idle"
   ```

2. Log intent to `/proc/root/log.md`.

3. Trigger:

   ```sh
   sbin/reboot
   ```

   `reboot` flock-probes `run/kernel.pid` first and refuses if no
   kernel holds it. On success it prints the kernel pid and exits;
   the running kernel does the rest. No response comes back — the
   next nudge will be post-boot reconcile or a driver event.

## Verify

- `cat /proc/root/log.md` — kernel logs `restart: draining nudges`
  → `triggering shutdown` → `stopped` → (new boot banner).
- `_discover_driver_specs` runs on import, so a new driver's slug
  appears in `/proc/` after reconcile.

For internals beyond this, read `memory/doc/KERNEL.md`.
