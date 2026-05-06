---
name: author-plan
description: How to draft a written plan to disk before executing multi-step work — goal, steps with verification, success criteria, and an explicit approval gate via status.md.
---

# Author a plan

## When this applies

You're about to do work that is:

- Multi-step (3+ shell actions, file edits, or tool spawns).
- Reversible-but-annoying-to-undo (touches config, fleet state, owner data).
- Worth a human glance before you start.

For one-shot commands (`ls`, `cat`, a single edit), skip this. Plans are
for work where "stop and read first" beats "run and hope."

## Where it lives

Per-PAI. Your own workspace, never another PAI's:

```
~/workspace/plans/<slug>/
    PLAN.md       # the plan
    status.md     # one word: draft | approved | executing | done | failed
    log.md        # append-only execution trace (skill execute-plan writes this)
    artifacts/    # anything the plan produces (optional)
```

`~/workspace/` resolves to `/var/lib/instances/<you>/workspace/`. Pick a
slug like `migrate-email-driver` or `add-calendar-pai` — short, kebab.

## The four required sections in PLAN.md

Terse beats prose. Each step must be a thing you can *check*, not a
vibe.

```markdown
# <one-line title>

## Goal
<2-3 sentences: what changes about the world when this is done>

## Constraints
- <thing you must not break>
- <invariant the operator cares about>

## Steps
1. <action>
   - verify: <command or file check that confirms success>
2. <action>
   - verify: <...>
3. ...

## Success criteria
- <observable end state, e.g. "paictl ls shows email-pai active">
- <...>

## Rollback
<one paragraph: if step N fails, how to get back to the start state>
```

Steps without `verify:` are not steps — they're wishes. Write the check
even if it's just `test -f /etc/config.yaml`.

## The drafting flow

```sh
SLUG=migrate-email-driver
mkdir -p ~/workspace/plans/$SLUG/artifacts
$EDITOR ~/workspace/plans/$SLUG/PLAN.md     # write the four sections
echo draft > ~/workspace/plans/$SLUG/status.md
: > ~/workspace/plans/$SLUG/log.md
```

Then surface a one-liner to the operator pointing at the path:

```sh
DAY=$(date +%F)
cat >> /var/spool/communication/messages/me/1/$DAY.md <<EOF

plan ready for approval: ~/workspace/plans/$SLUG/PLAN.md
edit status.md to "approved" to run, or reply with changes.
EOF
```

## The approval gate

You do not execute a plan you wrote without approval. Approval is a
file edit by the operator (or, in narrow autonomous cases you've been
told to handle, by you with a logged justification):

```sh
echo approved > ~/workspace/plans/$SLUG/status.md
```

Skill `execute-plan` refuses to run anything whose `status.md` is not
exactly `approved`. That refusal is the safety; don't route around it.

## Iterating on a draft

Plans are markdown. Edit `PLAN.md` freely while `status.md` is `draft`.
Once it's `approved`, treat it as frozen — if the operator wants
changes mid-flight, flip status back to `draft`, edit, re-approve.

## When NOT to author a plan

- The work is one shell command. Just run it.
- The work is exploration / reading. Plans are for *changes*.
- A skill already prescribes the procedure (e.g. `kernel-restart`,
  `grow-capability`) — follow the skill, don't shadow it with a plan.

## See also

- `execute-plan` — runs an approved plan and writes log.md.
- `/var/spool/communication/` — how the approval ping reaches the operator.
