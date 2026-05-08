---
name: schedule-reminder
description: How to schedule a one-shot or recurring timer with paicron — wake the PAI at a specific moment or on a cron, with the timezone convention spelled out.
---

# Schedule a reminder with paicron

## When this applies

Owner says any of:

- "remind me at 3pm", "wake me at noon", "ping me in 10 minutes"
- "every morning at 8 do X", "each Tuesday check Y"
- "in two hours, message Z"

Use **paicron** for these. It is *not* the same as:

- `bin/send-message` — fire an event right now (no delay).
- `bin/subagent spawn` — kick off background work to do *now*, not later.

paicron is the only tool that arms a future-firing timer.

## One-shot at a specific time

```sh
paicron start --slug wake-call --schedule "2026-05-04T12:00:00" \
    --description "wake owner for the call"
```

Stdout is the **full slug** (paicron auto-suffixes with the date so two
reminders sharing a base name don't collide). Read it back; you'll need
it to cancel.

When the moment hits, you'll be nudged with:

```yaml
reason: schedule fired
slug: wake-call-2026-05-04
context:
  schedule: "2026-05-04T12:00:00"
  description: wake owner for the call
```

Match by `slug` to know which reminder fired (you may have several
armed at once). Handle the wake there — message the owner, run a tool,
whatever the reminder was for.

## Recurring cron

```sh
paicron start --slug morning-recap --schedule "0 8 * * *" \
    --description "daily 8am recap"
```

Same wake shape; fires every match. Cancel with the full slug:

```sh
paicron stop morning-recap-2026-05-04
```

## Run a command at fire time without waking PAI

If the reminder is "say something out loud" or "run a script" — no PAI
reasoning needed at fire time — pass `--run`:

```sh
paicron start --slug noon-bell --schedule "2026-05-04T12:00:00" \
    --run "/usr/bin/say 'noon'"
```

The supervisor runs the subprocess; PAI gets nudged on `proc completed`
with the result (or `proc failed`) instead of `schedule fired`.

## Timezone convention — read this

`--schedule` ISO datetimes are interpreted as follows:

| Input shape | How it's interpreted |
|---|---|
| `2026-05-04T12:00:00`        (naive) | **local** wall-clock time |
| `2026-05-04T12:00:00-07:00`  (aware) | converted to local before firing |
| `2026-05-04T19:00:00Z`       (aware) | converted to local before firing |

So compute the time in the matching shape:

```sh
date +"%Y-%m-%dT%H:%M:%S"      # local, naive — fires when wall-clock matches
date +"%Y-%m-%dT%H:%M:%S%z"    # local, with offset — explicit and unambiguous
```

**Don't** pass `date -u` output as naive:

```sh
paicron start --slug bad --schedule "$(date -u +%FT%T)"   # WRONG
```

That string looks like UTC but is read as local — the timer fires at
your local wall-clock equal to the UTC string, hours off from intent.

(See `src/boot/timers.py:69-81` for the parser.)

## Computing relative times in the shell

macOS / BSD `date`:

```sh
date -v +5M  +"%Y-%m-%dT%H:%M:%S"     # 5 minutes from now
date -v +2H  +"%Y-%m-%dT%H:%M:%S"     # 2 hours
date -v +1d  +"%Y-%m-%dT%H:%M:%S"     # 1 day
```

GNU / Linux `date`:

```sh
date -d '+5 minutes' +"%Y-%m-%dT%H:%M:%S"
date -d '+2 hours'   +"%Y-%m-%dT%H:%M:%S"
date -d 'tomorrow 9am' +"%Y-%m-%dT%H:%M:%S"
```

One-liner:

```sh
paicron start --slug ping --schedule "$(date -v +5M +%FT%T)" \
    --description "5-minute ping"
```

## Inspect / cancel

```sh
paicron ls --status running     # every armed timer
paicron status <full-slug>      # full spec + log tail
paicron stop   <full-slug>      # cancel before fire
```

## When NOT to use paicron

- "Every Tuesday at 9am, ONE TIME, until I cancel" — paicron has no
  auto-cancel for cron expressions and no built-in TTL. Use a one-shot
  ISO for the next Tuesday and re-arm in the wake handler if the owner
  actually wants weekly.
- "Right now" — use `bin/send-message` or `bin/subagent spawn`. paicron is for
  future moments only.
