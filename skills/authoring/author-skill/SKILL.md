---
name: author-skill
visible_to: [root]
description: Use when writing a new SKILL.md (own initiative or via skill `grow-capability` with kind=skill). Covers description-line discipline, body shape, and when NOT to make a skill.
---

# Authoring a skill

A skill is a lazy-loaded procedural bundle. Root's prompt lists every installed skill as one line; root `cat`s the body when a description plausibly matches. **The description is the entire entry point.**

## File layout

```
<name>/
├── SKILL.md           # required: frontmatter + body
└── <extra files>      # optional templates/scripts, referenced by relative path
```

Directory name must equal `name:`.

## Frontmatter

```yaml
---
name: <kebab-case>             # = directory name
description: <one line>        # the trigger; concrete, specific
visible_to: [root]             # optional; restrict who sees the listing
---
```

### Description-line discipline

Root scans descriptions to decide what to load. A vague line gets loaded never or always — both wrong.

- Lead with **"Use when …"** naming the concrete trigger (an event kind, a file state, a request shape).
- Name the *output* if the skill produces one ("classify and decide restart vs surface").
- No filler ("helpful", "various", "stuff"). No restating the name.
- Compare: `diagnose-crash` (good — names the `proc_resolved` event + status), `grow-capability` (good — names the message shape and taxonomy it owns).

## Body

Terse procedural prose. The reader is under context pressure.

- Concrete commands over explanation. Show the `tail`, `cat`, `send-message` line.
- Tables for branching decisions (signal → action).
- Cross-reference shipped docs (`memory/doc/*.md`) and sibling skills by name; don't re-explain them.
- End with **"Read these next"** pointing at the docs and adjacent skills that complete the picture.

Reference shapes: `~/Projects/pairegistry/skills/diagnosing/diagnose-crash/SKILL.md` (tight action skill, table-driven), `~/Projects/pairegistry/skills/operating/grow-capability/SKILL.md` (longer because it owns a taxonomy — earns its length).

## When NOT to write a skill

- **Redocumenting a shipped doc.** If `memory/doc/KERNEL.md` already says it, link. Don't restate.
- **Redocumenting kernel-injected context.** Root's prompt already specifies the claudecode brief shape, send_message contract, FHS layout. Don't re-derive.
- **A single-use procedure.** One-shot work goes in the turn, not a skill.
- **Extending an existing skill with one note.** Edit in place; don't fork.

If the skill would be <15 lines of unique content, it's probably a doc note or a sibling-skill edit.

## Verification

After writing, read the description aloud and ask: *would root pick this for the trigger and not for unrelated work?* If both directions don't pass, rewrite the line.

For code hand-offs, use skill `execute-claudecode`.

## Read these next

- `memory/doc/FILESYSTEM_v3.md` — where skills live on disk and how `paiman install` places them.
- Sibling: `~/Projects/pairegistry/skills/diagnosing/diagnose-crash/SKILL.md`, `~/Projects/pairegistry/skills/operating/grow-capability/SKILL.md`.
