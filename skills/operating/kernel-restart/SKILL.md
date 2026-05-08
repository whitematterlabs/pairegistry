---
name: kernel-restart
description: Restart the kernel in-place (exec). Use after installing a new driver or making changes that require a fresh boot scan. Covers when to restart, how to trigger it, and what to expect.
---

# Kernel restart

A restart is an in-place `exec` — PID 1 re-execs itself, re-reads
`/etc/config.yaml`, re-discovers drivers in `/usr/lib/drivers/`, and
re-reconciles the fleet. All `/proc/` and `/sys/` state persists on
disk; running driver and PAI processes are restarted by reconcile.

**Do this when:**
- You installed a new driver (so `_discover_driver_specs` picks it up).
- `/etc/config.yaml` changed in a way that `kernel:reload_config`
  won't handle (e.g., a new PAI bundle that needs a fresh stitch).
- A driver or kernel helper is wedged and won't recover on its own.

**Do NOT do this** for routine config changes — `kernel:reload_config`
(emitted by `paiadd`, `paidel`, `paictl`) is enough for fleet
mutations. Restart is heavier: all running PAIs get a nudge gap.

## Procedure

1. **Check for in-flight work.** Scan `/proc/` for any PAI mid-turn
   (`status: running`). If found, wait or note the interruption in the
   log — nudges in flight will be dropped.

   ```sh
   grep -r "status: running" /proc/*/spec.yaml 2>/dev/null
   ```

2. **Log intent.**

   ```sh
   echo "[$(date -u +%FT%TZ)] kernel restart initiated — reason: <one line>" >> /proc/root/log.md
   ```

3. **Trigger the restart.**

   ```sh
   bin/paictl restart
   ```

   `paictl restart` emits a `kernel:restart` event. The kernel's event loop catches it, flushes pending
   events, and calls `os.execvp` on itself. You will not receive a
   response — your next nudge will be the post-boot reconcile or a
   `proc completed` from a re-started driver.

## What happens next

- The kernel re-reads `/etc/config.yaml` and re-runs `stitch_home`
  for every instance.
- `_discover_driver_specs()` scans `/usr/lib/drivers/*/events.yaml`
  fresh — newly installed drivers appear here.
- Reconcile restarts any process whose `active: true` spec didn't
  have a running pid.
- You (root) will be nudged once reconcile is done if anything
  requires attention.

## After installing a new driver

The full flow is:

```sh
# 1. Install the driver package
/sbin/paiman install /path/to/driver-source

# 2. Activate the process(es) it registered
bin/paictl start <driver-slug>-in    # if inbound
bin/paictl start <driver-slug>-out   # if outbound

# 3. Restart so the kernel discovers the new events.yaml
bin/paictl restart
```

After restart, `paictl start` was already written to
`/proc/<driver-slug>-*/spec.yaml active: true`, so reconcile brings
the driver up automatically.
