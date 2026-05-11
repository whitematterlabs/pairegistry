---
name: reload-config
visible_to: [root]
description: Use when a `kernel:reload_failed` event lands, or after editing `/etc/config.yaml` to trigger and verify a reconcile.
---

# Handling reload_config failures

`kernel:reload_config` runs `reconcile_from_config()` + `_reconcile_drivers()`
under a drained-nudge barrier. On exception, the kernel emits
`kernel:reload_failed` with `context.error` + `context.traceback`. No partial
application — config validation runs whole-file before any disk write.

Triggers (informational, not your concern unless you authored the edit):
`paictl start/stop` (flips `active:`), `paiadd`/`paidel`, owner hand-edits.

## When `kernel:reload_failed` lands

1. Read `context.traceback`. The exception class tells you the layer:
   - `ConfigError` → schema/validation in `/etc/config.yaml`. Message names
     the offending entry.
   - pid invariance error → existing PAI's `pid:` changed in config.
   - Anything else → bug in reconcile itself; surface to operator, do not edit.
2. `cat /etc/config.yaml` and find the entry.
3. Apply the **smallest** fix. Schema authority: `CONFIG_MANAGED_FIELDS` in
   `/usr/src/boot/config.py`. Required per entry: `name`, `description`.
   PIDs 1 (root) and 2 (pai) are reserved; auto-allocated pids are invariant.
4. Re-emit reload:
   ```
   bin/paictl reload
   ```
   (or any `paictl start/stop` — both emit `kernel:reload_config`.)
5. Verify: tail `proc/root/log.md` for `reload_config: done`. Check the
   affected entry's `proc/<name>/log.md` for
   `kernel: spec updated via reconcile (...)` or `spawned via reconcile`.
6. If it errors again with the same traceback, the fix wasn't complete.
   Don't loop — surface to operator.

## When NOT to fix yourself

- Provider/model changes — operator picks the model.
- Add/remove a PAI — use `/sbin/paiadd` / `/sbin/paidel`, not hand-edits.
- `wake_on` route changes that affect more than the broken entry.
- Driver coroutine crashes — those land in `proc/<driver-slug>/log.md`
  with status `failed`, not via `kernel:reload_failed`. Investigate the
  driver, don't touch config.

In those cases: append one line to
`var/spool/communication/messages/me/1/<today>.md` describing what's broken
and what decision is needed. Do not guess intent.

## Verification

After a successful reload, `proc/<name>/spec.yaml` has CONFIG_MANAGED_FIELDS
rewritten to match config; non-managed fields preserved. Status is healed:
`active: true` → running; `active: false` → resolved `stopped`.

Authoritative refs: `/usr/share/doc/KERNEL.md`,
`/usr/share/doc/FILESYSTEM_v3.md`, source at `/usr/src/boot/config.py` +
`/usr/src/boot/main.py` (`_handle_reload_config`, `_reconcile_drivers`).
Tool surface: see system-skill `kernel-tools`.
