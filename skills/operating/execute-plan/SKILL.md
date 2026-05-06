---
name: execute-plan
description: How to execute an approved plan from ~/workspace/plans/<slug>/ — refuse unless status.md is exactly "approved", walk steps, verify each, append to log.md, halt on failure.
---

# Execute a plan

## When this applies

You have a plan at `~/workspace/plans/<slug>/PLAN.md` whose `status.md`
contains exactly the word `approved`. Sibling skill `author-plan`
covers drafting and the approval gate.

This skill is a procedure, not a daemon. You run it; it's not a cron.

## The contract

```
status.md must read exactly: approved
PLAN.md must have:           Goal, Steps, Success criteria, Rollback sections
Each step must have:         a verify line
```

If any of those is missing, halt and tell the operator. Don't paper
over a malformed plan.

## The procedure

### 1. Refuse-or-proceed gate

```sh
SLUG=$1
DIR=~/workspace/plans/$SLUG
test -f $DIR/PLAN.md     || { echo "no plan at $DIR"; exit 1; }
[ "$(cat $DIR/status.md)" = "approved" ] || {
    echo "status is '$(cat $DIR/status.md)', not 'approved' — refusing"
    exit 1
}
```

Do not "interpret" `approve`, `Approved`, `approved.`. Exact match or
nothing. Strictness is the safety.

### 2. Flip status, stamp log

```sh
echo executing > $DIR/status.md
echo "## $(date -Iseconds) start" >> $DIR/log.md
```

### 3. Walk steps

For each numbered step in `PLAN.md`:

1. Append `### step N: <one-line summary>` to `log.md`.
2. Run the step's action.
3. Run the step's `verify:` check.
4. Append the verify result (output or `ok`) to `log.md`.
5. If verify fails, jump to **failure path**.

One step at a time. Do not parallelize unless the plan says so
explicitly. If a step is "spawn coder for X," use `bin/subagent spawn`
and *wait* for `proc completed` before moving on — don't fire-and-forget
mid-plan.

### 4. Success path

After the last step verifies:

```sh
echo done > $DIR/status.md
echo "## $(date -Iseconds) done" >> $DIR/log.md
```

Then check the **Success criteria** block — these are *additional*
end-state checks beyond per-step verifies. Append each result to
`log.md`. If any fail, status is still `done` (the steps ran) but
surface a note to the operator: criteria did not hold, see log.

One-liner to operator inbox:

```sh
DAY=$(date +%F)
echo "plan $SLUG: done — see ~/workspace/plans/$SLUG/log.md" \
    >> /var/spool/communication/messages/me/1/$DAY.md
```

### 5. Failure path

A step's verify failed. Do not continue. Do not retry silently.

```sh
echo failed > $DIR/status.md
echo "## $(date -Iseconds) FAILED at step $N" >> $DIR/log.md
echo "<verify output>"                        >> $DIR/log.md
```

Then: read PLAN.md's **Rollback** section and execute it. Append each
rollback action's outcome to `log.md`. Surface to operator with the
plan path — they decide whether to retry, revise, or abandon:

```sh
DAY=$(date +%F)
echo "plan $SLUG: FAILED at step $N — see ~/workspace/plans/$SLUG/log.md" \
    >> /var/spool/communication/messages/me/1/$DAY.md
```

## Resumption after kernel restart

If the kernel restarts mid-execution, `status.md` will read `executing`
and `log.md` will show which steps already finished (each has a
verified-ok line). On restart, do not auto-resume. Surface a one-liner
to the operator: "plan $SLUG was mid-execution at restart, last
completed step N — resume? (flip status back to approved)". They
decide.

## When NOT to use this skill

- `status.md` is `draft` — author isn't done. Don't help by approving
  your own plan unless the operator told you that was fine.
- The plan is a one-step plan. Just do the thing.
- Work that's actually a long-running service. That's a driver
  (`author-driver`), not a plan.

## See also

- `author-plan` — the drafting side; defines the file shape this skill consumes.
- `grow-capability` — when a step needs a tool that doesn't exist yet, that's a coder spawn, not a plan failure.
