---
name: restart-driver
visible_to: [root]
description: Bounce a crashed kernel-owned driver (NOT a PAI). Use when a driver slug has `failed` status with a transient cause.
---

# Restart a driver

Drivers are kernel-owned, code-registered processes (discovered from `events.yaml` `processes:` blocks under `/usr/lib/drivers/`). They have their own lifecycle, distinct from instance PAIs: lifecycle = the `active:` bool in `/proc/<slug>/spec.yaml`. `paictl` flips it and emits `kernel:reload_config`; the kernel's `_reconcile_drivers` reacts. See `memory/doc/KERNEL.md`, `memory/doc/KERNEL_EVENTS.md`, `memory/doc/FILESYSTEM_v3.md`.

## When to restart (transient)

- `/proc/<slug>/status` == `failed` AND
- `tail -n 50 /proc/<slug>/log.md` shows: network blip, DB busy/locked, fs race, transient file lock, timeout against a flaky external surface.

## When NOT to restart (structural — escalate, don't loop)

- `ImportError` / `ModuleNotFoundError` — code is broken; spawn a coder via `grow-capability`.
- `KeyError` / `AttributeError` on schema fields — manifest or upstream contract drifted.
- Same traceback within ~60s of a prior restart — looping, not transient.
- Driver missing from `_discover_driver_specs()` walk (no proc spawns at all) — `events.yaml` problem, not a restart case.

## Procedure

```
tail -n 50 /proc/<slug>/log.md          # confirm transient
paictl stop <slug>                       # active: false, reload, reconcile cancels task
paictl start <slug>                      # active: true, reload, reconcile respawns task
sleep 2 && cat /proc/<slug>/status       # expect: running
tail -n 5 /proc/<slug>/log.md            # expect: "kernel: restarted" + driver's own startup line
```

`paictl reload` alone will NOT restart a healthy-but-stuck driver — `_reconcile_drivers` only acts on `active:` mismatches. Stop then start.

## After editing driver source

`paictl stop`/`start` cycles the driver **task** (asyncio coroutine), not the kernel's Python module table. The driver's `inbound.py` is imported into `sys.modules` once at kernel boot; restarting the task re-enters the same imported module. So edits to a driver's `.py` files — including reinstalls via `paiman install drivers/<name>` — are NOT picked up by `paictl start/stop`. Symptom: log lines still match the old code after stop/start.

Reboot the kernel to reload driver modules:

```
sbin/reboot     # re-execs the kernel in place; emits kernel:restart
```

This is correct for: edits to `inbound.py`, edits to `events.yaml` that change processes, new versions installed via `paiman install`. Not needed for: `/etc/config.yaml` edits (reload-config covers those), `active:` flips (paictl does it).

Also clear `__pycache__` under `usr/lib/drivers/<name>/` before reboot if a stale `.pyc` is shadowing your edit — paiman copies sources but Python's bytecode cache lives in the runtime dir.

## Verify it came back

- `/proc/<slug>/status` reads `running` (not `failed`/`cancelled`).
- Driver-emitted events resume in `/var/log/kernel/kernel.log` (grep `driver started: <slug>`).
- For event-producing drivers: an event with `source: <driver>` appears in `/home/events/` within a normal duty cycle.

## Log the action

Append to `/proc/root/log.md`:

```
restarted <slug> after <one-line cause>
```

## Escalate when

- Second `failed` within 60s of restart → spawn a coder via `grow-capability` with the traceback; do not loop.
- Structural signal (see above) on first read → skip restart, go straight to `grow-capability`.
- Owner-visible breakage (messages, email, calendar driver down) → also append to `/var/spool/communication/messages/me/1/<today>.md`:
  `[<HH:MM>] root: <slug> looping on <cause>; needs your eyes`.
