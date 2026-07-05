---
name: execute-claudecode
visible_to: [root]
description: Shell out to `claude -p` (ClaudeCode headless) to build an artifact — the canonical build mechanism whenever you'd reach for multi-step bash, heredoc'd Python, or a "quick helper." The exploration loop lives inside the subprocess; you write the brief, fire it, ship.
---

# Executing claudecode

Direct headless invocation of ClaudeCode. This is **the** build path
for root and PAIs: anything that would otherwise be a multi-step bash
pipeline, a heredoc'd Python one-shot, or a "let me just write a
quick helper" belongs in a claudecode brief. You don't hand-author
the artifact; you describe it and let the subprocess build it.

The brief format, ≤80-word limit, shape contract, and post-build
help-page discipline are already covered by root's prompt — defer to
it. This page adds only the nuances of `claude -p` itself.

For the brief shape, type taxonomy, and post-ship help page rules,
read `memory/doc/SUBAGENT_BUNDLES.md` and the "Building things"
section of root's prompt. **Do not redocument them here.**

## STOP — do not scout before invoking

Before shelling out, do **not** probe the environment, sanity-check
APIs, read library docs, or run "one quick command to make sure."
That is the subprocess's job — it has the file tools, the harness,
and the iteration budget. Pre-scouting burns kernel turns on work
the subprocess redoes anyway, and shrinks the brief you'd otherwise
write.

The only check before invoking: **do you know what artifact you want
and where it lands?** If yes, fire.

## Point the subprocess at relevant skills

Claudecode spawns with no system prompt and no knowledge of PAI conventions. Cold, it will reinvent patterns we already have (e.g. roll its own polling loop instead of using `schedule-reminder`'s one-shot `paicron` + `reason: schedule fired` wake).

Before firing, `ls memory/skills/` and pick the 1–3 skills whose names match the work. Add a `read_first:` line to the brief listing their paths. The subprocess `cat`s those first, then builds.

```
type: bin
name: foo
need: …
why: …
shape: foo --bar
read_first: memory/skills/schedule-reminder/SKILL.md, memory/skills/<other>/SKILL.md
```

This is a kernel-side scan (one `ls`, no `cat`), not pre-scouting the artifact. It doesn't violate the "no pre-scout" rule below — you're picking pointers, not doing the subprocess's work. Skip the field if nothing in `memory/skills/` plausibly applies.

## Driver briefs — author-driver is mandatory

For **every** `type: driver` brief, `read_first:` **must** include
`memory/skills/author-driver/SKILL.md`. No exceptions — not "if the
surface has history", not "if it polls", always. Claudecode spawns
with no system prompt; without that skill loaded it will reinvent
the driver shape (cursors, async contract, event batching,
backfill avoidance, sidecar layout) from scratch and get at least
one of them wrong.

Concretely, the `read_first:` line on a driver brief looks like:

```
read_first: memory/skills/author-driver/SKILL.md[, …other skills]
```

If you catch yourself writing a `type: driver` brief without that
path in `read_first:`, stop and add it before firing.

Additionally, if the surface has history (calendar, email,
messages, contacts, photos, files — anything with a past), pin the
no-backfill rule in the brief body too, since it's the single
biggest failure mode:

```
no-backfill: first run establishes a cursor at "now" under
  /sys/drivers/<name>/ and emits zero events for items older than
  that. backfill is a separate bin, not driver boot behavior.
```

The cursor shape per surface is in `author-driver` §"Don't backfill
history. Start from 'now.'" — the subprocess will reach it via the
mandatory `read_first:` above.

## Invocation

```sh
claude -p --dangerously-skip-permissions "<brief>"
```

- Single shell call. No timeout — long sessions are fine.
- The brief is the *entire* context; the subprocess has no system
  prompt of its own. Include type, name, need, why, shape (hard
  contract for bins), target paths, and the subprocess's own
  definition of done.
- One artifact per invocation.

## After it returns

1. Skim the diff and the subprocess's report.
2. If it says done is met, trust it. **Do not re-run its
   verification.** If `done:` was right, the report is right; if it
   was wrong, fix `done:` and re-fire — don't patch by hand.
3. Ship: install / activate (`paiman install`, `paictl start`,
   `paiadd`). That's your step, not the subprocess's.
4. Write the help page at `memory/doc/built/<name>.md` per root's
   prompt.

## Boundaries

- Do **not** invoke or activate the built thing from inside the
  brief — that's the ship step.
- Do **not** read kernel internals unless the brief is genuinely
  kernel work.
- If the brief is ambiguous, pick the simplest reasonable read over
  a clarification round-trip.

