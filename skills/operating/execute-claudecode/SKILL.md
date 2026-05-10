---
name: execute-claudecode
description: Compose a brief and shell out to `claude -p` (ClaudeCode headless) to build a bin / driver / skill / subagent / pai-bundle / prompt. You are not the coder — the real intelligence and harness live inside the `claude -p` subprocess. You write the brief, fire it, verify the result, ship.
---

# Executing claudecode

When you need to build a non-trivial artifact (bin, driver, skill,
subagent, pai-bundle, prompt) the standard move is to invoke
`claude -p` (ClaudeCode headless) yourself. You compose the brief,
shell out, read the result, verify, ship. You are not the coder —
the real intelligence and harness live inside the `claude -p`
subprocess.

If the task is small enough to do without `claude -p` (a one-line
edit, an obvious config tweak), don't bother with this skill. Just
do it.

If the task is purely investigative (read, summarize, no artifact),
spawn the `scout` subagent instead — it's cheaper and won't
pollute the build lane.

## The brief

Compose a brief with these fields:

```
type: <bin | driver | skill | subagent | pai-bundle | prompt>
name: <package / file name>
need: <what it should do>
why:  <reason / requester's ask>
shape: <for bin: exact CLI invocation; for others: omit or describe usage>
```

`type:` controls where and how the work is written. `shape:` is a
**hard contract** for bin tools — the caller will invoke it with
that exact shape. For other types, treat it as usage documentation.

## How to invoke

Single shell call. No timeout — long sessions are fine.

```sh
claude -p --dangerously-skip-permissions "<full brief: type, name,
need, why, shape, file paths, expected behavior, verification step>"
```

Be specific. Hand off everything Claude would need to work without
asking follow-ups: target paths, constraints, how to verify, what
files matter.

## Workflow

1. **Compose the brief.** All fields above. Specific paths, expected
   behavior, verification step.
2. **Invoke `claude -p`.** Let it run.
3. **Read what it produced.** Spot-check the diff. Run the
   verification yourself. If wrong, send a follow-up `claude -p`
   with the specific correction needed.
4. **Ship.** Install / activate as appropriate (`paiman install`,
   `paictl start`, `paiadd`, etc.) — that's your job, not Claude's.

## By type — where things land

### `type: bin`
`/usr/lib/bin/<name>` (chmod +x). Lands in shared `/usr/bin/` for
all PAIs. Shell or Python — whichever is shorter. One file.
Verification: invoke it once with sensible test input.

### `type: driver`
`/usr/lib/drivers/<name>/` with `package.yaml`, `events.yaml`,
`__init__.py`, and `inbound.py` / `outbound.py` as needed. After
build: `sbin/paiman install /usr/lib/drivers/<name>/` then
`bin/paictl start <name>-in` and `bin/paictl restart`.

Drivers own the on-disk shape of an external surface (messages,
email, calendar, contacts). They emit events by writing YAML to
`/run/pai/events/` or calling `P.emit_event(...)` from the driver
process.

### `type: skill`
`/usr/lib/skills/<name>/SKILL.md` with frontmatter (`name:`,
`description:`). Markdown, actionable — exact commands to run,
files to read, things to check.

### `type: subagent`
`/usr/lib/subagents/<name>/` with `package.yaml` and `prompt.md`.
The prompt is injected as the system prompt when spawned: role,
brief format, outputs, termination condition (`subagent kill`).

### `type: pai-bundle`
`/usr/lib/pais/<name>/` with `package.yaml` (defaults: provider,
model, wake_on globs) and `prompt.md` (one screen max — who it is,
what surface it manages, how it responds). Instantiate with
`sbin/paiadd <name>`.

### `type: prompt`
`/usr/share/prompts/<name>/prompt.md`. Plain markdown, no
frontmatter required. The personality a PAI instance loads.

## Scope discipline

- **Bin-first default.** One-shot questions ("what's on my calendar",
  "book a reservation", "post a tweet") are bins on top of an
  existing primitive driver, not new drivers. Drivers exist for
  *primitive surfaces* (app ABI, system framework, I/O channel,
  shared long-lived session) where collapsing into an existing
  primitive would lose native event hooks.
- **One artifact per brief.** If `type: bin`, don't also write a
  driver. Promote later if state earns it.
- **No premature abstraction.** No speculative configuration. No
  future-proofing for hypothetical needs.
- **Verify end-to-end.** For features and drivers, exercise the real
  path — not just imports.

## Boundaries

- Do **not** invoke or activate the thing being built from inside
  the `claude -p` brief. That's your follow-up step after it
  finishes.
- Do **not** read kernel internals (`/usr/src/boot/`, `/boot/`)
  unless the brief is genuinely about kernel work. Userspace
  artifacts don't need them.
- If the brief is ambiguous, prefer the simplest reasonable
  interpretation over a clarification round-trip.
