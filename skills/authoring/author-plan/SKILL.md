---
name: author-plan
description: Draft a checkpointed multi-step plan at ~/workspace/plans/<slug>/ that sibling skill execute-plan will consume. Four required sections, per-step verify lines, explicit approval gate via status.md.
---

# Author a plan

Sibling `execute-plan` runs what this skill produces. The file shape below is exactly what it expects — drift and it refuses.

## When to author one

- 3+ shell actions, edits, or subagent spawns.
- Touches config, fleet state, or owner data — annoying to undo.
- Long enough that a nudge or kernel restart could hit mid-flight (plans survive both; status.md is the checkpoint).

Skip for one-shot commands, pure reading, or work a dedicated skill already prescribes (`grow-capability`, etc.).

## Layout

Per-PAI, in your own workspace:

```
~/workspace/plans/<slug>/
    PLAN.md       # the plan (shape below)
    status.md     # one word: draft | approved | executing | done | failed
    log.md        # execute-plan appends here; you create empty
    artifacts/    # optional outputs
```

`~/workspace/` resolves to `/var/lib/instances/<you>/workspace/`. Slug is short kebab — `migrate-email-driver`, `add-calendar-pai`.

## PLAN.md shape — exactly four sections

execute-plan parses for these headers. Rename them and it halts.

```markdown
# <one-line title>

## Goal
<2-3 sentences: what about the world is different when this finishes>

## Steps
1. <action>
   - verify: <shell check or file test that returns success>
2. <action>
   - verify: <...>
3. ...

## Success criteria
- <end-state check, beyond per-step verifies>
- <...>

## Rollback
<one paragraph: if a step fails mid-flight, how to return to the start state>
```

Rules:

- **Every step needs a `verify:` line.** A step without a check isn't a step, it's a wish. `test -f /etc/config.yaml`, `paictl ls | grep -q email-pai`, `[ "$(cat status.md)" = approved ]` — anything that exits non-zero on failure.
- **Steps are sequential by default.** execute-plan walks them one at a time. If a step legitimately parallelizes, say so in the action line.
- **"Spawn coder for X" is a valid step.** execute-plan routes it through `grow-capability` → `execute-claudecode` and waits for `--done`. Write the verify against the artifact the coder produces, not the spawn itself.
- **Success criteria ≠ per-step verifies.** Per-step is "did this step do its thing." Success criteria is "is the goal actually achieved" — checked after all steps complete.
- **Rollback is mandatory, even if it's "nothing to undo, re-run from step 1."** execute-plan reads this section on failure.

## Drafting flow

```sh
SLUG=migrate-email-driver
mkdir -p ~/workspace/plans/$SLUG/artifacts
$EDITOR ~/workspace/plans/$SLUG/PLAN.md
echo draft > ~/workspace/plans/$SLUG/status.md
: > ~/workspace/plans/$SLUG/log.md
```

Surface for approval:

```sh
send-message --to 1 --content "plan ready: ~/workspace/plans/$SLUG/PLAN.md — edit status.md to 'approved' to run, or reply with changes"
```

## Approval gate

Never execute a plan you authored without approval. Approval is a file edit by the owner:

```sh
echo approved > ~/workspace/plans/$SLUG/status.md
```

execute-plan refuses anything whose `status.md` is not exactly `approved` (not `Approved`, not `approved\n# notes`). That refusal is the safety — don't route around it. In narrow autonomous cases where you've been told to self-approve, log the justification in `log.md` before flipping status.

## Iterating

While `status.md` is `draft`, edit freely. Once `approved`, treat as frozen — if changes are needed mid-flight, flip back to `draft`, edit, re-approve.

## When NOT to author

- Work is one shell command.
- Work is exploration or reading (no state changes).
- A skill already prescribes the procedure — follow it, don't shadow it.
- Long-running service work — that's a driver (`author-driver`), not a plan.

## See also

- `execute-plan` — consumes the file shape above.
- `grow-capability` — step needs a tool that doesn't exist → coder spawn inside the plan, not a plan failure.
