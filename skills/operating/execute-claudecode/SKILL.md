---
name: execute-claudecode
visible_to: [root]
description: Compose a brief and shell out to `claude -p` (ClaudeCode headless) to build a bin / driver / skill / subagent / pai-bundle / prompt. You are not the coder and you are not the scout — the real intelligence, harness, and exploration loop live inside the `claude -p` subprocess. You write the brief, fire it, ship the result.
---

# Executing claudecode

When you need to build a non-trivial artifact (bin, driver, skill,
subagent, pai-bundle, prompt) the move is to invoke `claude -p`
(ClaudeCode headless). You compose the brief, shell out, ship.
You are not the coder. You are not the scout. The real intelligence
and the cheap exploration loop both live inside the `claude -p`
subprocess.

If the task is purely investigative (read, summarize, no artifact),
spawn the `scout` subagent instead — it's cheaper and won't
pollute the build lane.

## STOP — do not scout before invoking

Before you shell out to `claude -p`, do **not**:

- Probe the environment (`pip list`, `python -c "import …"`,
  `which`, `ls` of system paths to see if something is installed).
- Sanity-check APIs, bindings, file shapes, or sample data.
- Read library source, framework docs, or example payloads.
- Run "just one quick command to make sure" anything.

All of that is `claude -p`'s job. It has the file tools, the harness,
and the iteration budget to do it well. If you do it from root, you
burn kernel turns on work the subprocess will redo anyway, and you
fool yourself into composing a narrower brief than the subprocess
would have written for itself.

The only thing you check before invoking is: **do I know what
artifact I want and where it lands?** If yes, brief and fire.

## The brief

Compose a brief with these fields:

```
type: <bin | driver | skill | subagent | pai-bundle | prompt>
name: <package / file name>
need: <what it should do>
why:  <reason / requester's ask>
shape: <for bin: exact CLI invocation; for others: omit or describe usage>
done: <the subprocess's own definition of done — what it must
       verify before returning>
```

`type:` controls where and how the work is written. `shape:` is a
**hard contract** for bin tools — the caller will invoke it with
that exact shape. `done:` is what the subprocess must satisfy
itself of before it returns; it is not a checklist you re-run.

## How to invoke

Single shell call. No timeout — long sessions are fine.

```sh
claude -p --dangerously-skip-permissions "<full brief: type, name,
need, why, shape, file paths, done criteria>"
```

Be specific. Hand off everything Claude would need to work without
asking follow-ups: target paths, constraints, what counts as done,
what files matter.

## Workflow

1. **Compose the brief.** All fields above, including `done:`.
2. **Invoke `claude -p`.** Let it run.
3. **Read what it produced.** Skim the diff and the subprocess's
   own report. If it returned saying done is met, trust it. If it
   reported a blocker or returned something obviously wrong, send
   a follow-up `claude -p` with the specific correction. Do not
   re-run its verification yourself.
4. **Ship.** Install / activate as appropriate (`paiman install`,
   `paictl start`, `paiadd`, etc.) — that's your job, not Claude's,
   and it's the only post-subprocess action you take.

## By type — where things land

### `type: bin`
`/usr/lib/bin/<name>` (chmod +x). Lands in shared `/usr/bin/` for
all PAIs. Shell or Python — whichever is shorter. One file.

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
- **Verification belongs in `done:`, not in your hands.** Write
  end-to-end verification into the brief's `done:` field. The
  subprocess runs it. You don't.

## Boundaries

- Do **not** scout, probe, or verify before invoking. (See "STOP"
  above.) The subprocess does its own discovery.
- Do **not** invoke or activate the thing being built from inside
  the `claude -p` brief. That's your follow-up ship step.
- Do **not** re-run the subprocess's verification after it returns.
  If `done:` was right, trust the report; if it wasn't, fix `done:`
  and re-fire.
- Do **not** read kernel internals (`/usr/src/boot/`, `/boot/`)
  unless the brief is genuinely about kernel work. Userspace
  artifacts don't need them.
- If the brief is ambiguous, prefer the simplest reasonable
  interpretation over a clarification round-trip.
