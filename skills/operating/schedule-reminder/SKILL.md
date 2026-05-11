---
name: schedule-reminder
description: Schedule a one-shot reminder (timer with no --run) that nudges the calling PAI when it fires.
---

# Schedule a reminder

A **reminder** = `paicron start --schedule <ISO>` with **no** `--run`. When the
moment hits, the calling PAI gets nudged with `reason: schedule fired`.

Not this skill:
- Recurring cron job (`--schedule '0 8 * * *' --run ...`) — different shape.
- One-shot background job (`--run ... ` no schedule) — fires now, not later.
- Send a message now — `bin/send-message`.

## Invoke

```sh
paicron start --slug wake-call --schedule "2026-06-14T15:00:00" \
    --description "wake the owner for the 3pm call"
```

Stdout: the **full slug** (paicron appends `-YYYY-MM-DD`, e.g.
`wake-call-2026-05-11`; on same-day collision it appends a full
`-YYYY-MM-DDTHH-MM-SS` timestamp instead). For one-shot reminders it also
prints `fires at <resolved local ISO>` so you can confirm parsing.

Save the slug; you need it to cancel.

`--restart` defaults to unset (kernel treats one-shot timers as fire-and-resolve;
do not pass `--restart` for reminders).

## Wake shape

```yaml
reason: schedule fired
slug: wake-call-2026-05-11
context:
  schedule: "2026-06-14T15:00:00"
  description: wake the owner for the 3pm call
```

Match by `slug` (you may have several armed). Handle in the wake — message
the owner, run a tool, whatever the reminder was for.

## Timezones

`--schedule` ISO datetimes:

| Input | Interpreted as |
|---|---|
| `2026-06-14T15:00:00`        (naive) | local wall-clock |
| `2026-06-14T15:00:00-07:00`  (aware) | converted to local |
| `2026-06-14T22:00:00Z`       (aware) | converted to local |

Don't pass `date -u` output as a naive string — it looks like UTC but is read
as local. Parser: `src/boot/timers.py:parse_schedule`.

## Relative times

macOS / BSD:

```sh
date -v +5M  +"%Y-%m-%dT%H:%M:%S"     # 5 minutes
date -v +2H  +"%Y-%m-%dT%H:%M:%S"     # 2 hours
date -v +1d  +"%Y-%m-%dT%H:%M:%S"     # tomorrow, same wall-clock
```

GNU:

```sh
date -d '+5 minutes'    +"%Y-%m-%dT%H:%M:%S"
date -d 'tomorrow 9am'  +"%Y-%m-%dT%H:%M:%S"
```

One-liner:

```sh
paicron start --slug ping --schedule "$(date -v +5M +%FT%T)" \
    --description "5-minute ping"
```

## Inspect / cancel

```sh
paicron ls --status running     # every armed timer
paicron status <full-slug>      # spec + log tail
paicron stop   <full-slug>      # cancel before fire
```

## See also

- `memory/doc/KERNEL.md` — event loop, nudge dispatch, timer wheel.
- `memory/doc/FILESYSTEM_v3.md` — `/proc/<slug>/` layout that paicron writes.
