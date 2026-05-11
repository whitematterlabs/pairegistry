---
name: diagnose-crash
description: Use when a `proc_resolved` event arrives with status=failed (or `/proc/<slug>/status` reads `failed`) to classify the cause and decide restart vs surface.
---

# Diagnose a crashed proc

Output: one line, `<slug> failed: <innermost-exception> — <restart|surface|coder>`.

## Procedure

1. `tail -n 80 /proc/<slug>/log.md` — locate the last traceback. Note the innermost exception type + message.
2. `cat /proc/<slug>/spec.yaml` — confirm `kind` and which driver/PAI owns it.
3. Classify:

   | Signal | Decision |
   |---|---|
   | `sqlite3.OperationalError: database is locked` | restart |
   | `BlockingIOError`, `FileLockError`, transient `OSError` on a socket | restart |
   | `requests.ConnectionError`, DNS failure, 5xx from upstream | restart |
   | `ImportError`, `ModuleNotFoundError` | surface |
   | `AttributeError` / `KeyError` on an event payload field | surface |
   | `PermissionError` on a path root can't grant | surface |
   | Same innermost exception 3+ times in the last hour | coder |
   | Corrupt cursor under `/sys/drivers/<slug>/` (JSON decode error, schema mismatch) | surface — propose deleting the cursor |

4. Append to `/proc/root/log.md`:
   ```
   [HH:MM] <slug> failed: <innermost-exception> — <decision>
   ```

## Hand-off

- `restart` → invoke skill `restart-driver`.
- `surface` → one line to the owner's inbox (`/var/spool/communication/messages/me/1/<today>.md`) naming the exception and `/proc/<slug>/log.md`. Never paste the traceback.
- `coder` → spawn a subagent with the failing file path + last traceback; goal: root cause + patch.

## Notes

- Event shape, status values, and `/proc/<slug>/` layout: `memory/doc/KERNEL_EVENTS.md`, `memory/doc/FILESYSTEM_v3.md`.
- Loop-detection and restart budgets: `memory/doc/SELF_HEALING.md`.
