---
name: execute-plan
description: Execute an approved plan at ~/workspace/plans/<slug>/. Refuse unless status.md is exactly "approved". Walk steps one at a time, verify each, append to log.md, halt on failure.
---

# Execute a plan

Sibling `author-plan` defines the file shape. This skill consumes it.

## Contract

```
status.md           exactly: approved
PLAN.md sections    Goal, Steps, Success criteria, Rollback
each step has       a verify: line
```

Any missing piece → halt, tell the owner. Do not paper over a malformed plan.

## Procedure

### 1. Gate

```sh
SLUG=$1
DIR=~/workspace/plans/$SLUG
test -f $DIR/PLAN.md || { echo "no plan at $DIR"; exit 1; }
[ "$(cat $DIR/status.md)" = "approved" ] || {
    echo "status is '$(cat $DIR/status.md)', not 'approved' — refusing"; exit 1; }
```

Exact match. Not `approve`, not `Approved`, not `approved.`.

### 2. Stamp

```sh
echo executing > $DIR/status.md
echo "## $(date -Iseconds) start" >> $DIR/log.md
```

### 3. Walk steps

For each numbered step:

1. Append `### step N: <summary>` to `log.md`.
2. Run the action.
3. Run the step's `verify:` check.
4. Append result (output or `ok`) to `log.md`.
5. Verify failed → failure path.

One step at a time. No parallelism unless the plan says so. For "spawn coder for X" steps, use `subagent spawn` and wait for `subagent:response --done` before continuing — no fire-and-forget mid-plan.

### 4. Success

```sh
echo done > $DIR/status.md
echo "## $(date -Iseconds) done" >> $DIR/log.md
```

Then run each **Success criteria** check (end-state, beyond per-step verifies). Append results to `log.md`. If any fail, status stays `done` but surface a note: criteria did not hold, see log.

Notify owner via `send-message --to 1 --content "..."`. `send-message --help` for flags.

### 5. Failure

```sh
echo failed > $DIR/status.md
echo "## $(date -Iseconds) FAILED at step $N" >> $DIR/log.md
echo "<verify output>"                        >> $DIR/log.md
```

Execute PLAN.md's **Rollback** section. Append each rollback outcome to `log.md`. Surface to owner with the plan path; they decide retry/revise/abandon.

## Resumption after kernel restart

If kernel restarted mid-execution: `status.md` reads `executing`, `log.md` shows last ok'd step. Do not auto-resume. Surface one-liner to owner: "plan $SLUG was mid-execution at restart, last completed step N — resume? (flip status back to approved)".

## When NOT to use

- `status.md` is `draft` — author isn't done. Don't approve your own plan.
- One-step work. Just do it.
- Long-running service work. That's a driver (`author-driver`), not a plan.

## See also

- `author-plan` — drafting side.
- `grow-capability` — step needs a tool that doesn't exist yet → coder spawn, not a plan failure.
