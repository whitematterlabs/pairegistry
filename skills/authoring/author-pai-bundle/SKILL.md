---
name: author-pai-bundle
visible_to: [root]
description: Howto for authoring a new PAI bundle (kind:pai) — package.yaml, prompt.md, paiadd to instantiate. Reference when adding a new fleet member.
---

# Authoring a PAI bundle

A PAI bundle is the **template** a fleet member is instantiated from.
Scope check: if there's no driver yet for the surface the PAI wakes
on, build the driver first — a bundle without its driver wakes on
nothing.

## The three layers

| Layer | Path | Lifetime |
|---|---|---|
| **Bundle** (template) | `/usr/lib/pais/<name>/` | immutable post-install |
| **Instance** (configured copy) | `/var/lib/instances/<inst>/` | per-PAI sacred state |
| **Process** (running) | `/proc/<inst>/` | per-boot runtime |

`paiadd <bundle>` reads the template, writes a `/etc/config.yaml`
entry, creates the instance dir, and emits `kernel:reload_config`.
The kernel reconciles → `/proc/<inst>/` appears → the PAI wakes.

Edits to the bundle do not retroactively touch existing instances'
memory or drafts; they do shape the next nudge's prompt.

## Where source lives

**Pairegistry** (`~/Projects/pairegistry/pais/<name>/`) when the bundle
is general-purpose — install via `paiman install <name>`. **Local**
(`/usr/lib/pais/<name>/` directly) when owner-specific. Don't keep
both copies of the same name.

## Layout

```
pais/<name>/
├── package.yaml
└── prompt.md
```

That's it. Drivers and skills are **declared, not vendored** —
`paiman` resolves them.

## package.yaml

Canonical shape (see `pais/email-pai/` and `pais/whatsapp-pai/`):

```yaml
name: email-pai
kind: pai
version: 0.1.0
description: Email-handling PAI — triages and replies to incoming email
provider: deepseek
model: deepseek-v4-pro
prompt: prompt.md
wake_on:
  - email:new
  - email:backlog
  - email:draft_failed
deps:
  - email          # driver
  - inbox          # bin
home:
  links:
    - link: communication/email
      target: var/spool/communication/email
    - link: drafts
      target: var/spool/communication/email/drafts
```

**Required:** `name`, `kind: pai`, `version`, `prompt`, `wake_on`.
**`deps`** lists driver/bin/skill packages by name; `paiman install`
walks them. **`provider`/`model`** become the instance default at
`paiadd` time (overridable per-instance in `/etc/config.yaml`).

### `wake_on`

Names of driver events. Pick the narrowest set — a PAI woken on
`email:*` will also wake on `email:sync_finished` and burn turns.
Existing bundles' `wake_on:` is the reference.

### `home.links`

By default the kernel stitches only the universals (`bin`, `inbox`,
`workspace`, `memory/*`, `tmp`) into the instance's home. The bundle
declares the channel slice this PAI sees.

Rules:
- `link` is a path under `$HOME`; collisions with universals reject
  at stitch time.
- `target` is relative to `PAI_ROOT`; must stay inside it.
- **Narrow is correct.** Email-pai gets `communication/email` and
  `drafts`, not `communication/`. Isolation is the point — email-pai
  shouldn't see iMessage and vice versa.

### Driver mounting via `deps:`

Listing a driver in `deps:` does two things, not one: `paiman` installs
it, **and** the kernel stitches that driver's own `home.links` into this
PAI's home. There is no way to depend on a driver without mounting it,
and no per-instance override — every instance of this bundle mounts the
same driver set. The mounted set is `deps ∩ installed-drivers` (a
`fallback: true` PAI instead mounts *every* installed driver). If a
driver's link name collides with one of yours here, stitch fails hard.
Full policy: `memory/doc/FILESYSTEM_v3.md` → "Driver mounting".

## prompt.md — the role prompt

The kernel assembles each nudge as `<custom>` (your prompt) plus
boilerplate blocks (`<owner>`, `<memory-usage>`,
`<capability-escalation>`, `<self-notes>`, `<fleet>`,
`<operating-instructions>`, `<bins>`, `<skills>`). You own
`<custom>` only — don't restate what the boilerplate already says.

### Discipline

- **Never use the owner's name.** Say "the owner". Bundles ship
  generically; the name is injected via `<owner>` boilerplate.
- **Terse.** Concrete file shapes > prose. The reader is a model
  parsing under turn pressure, not a human onboarding.
- **No kernel-injected context restated.** Don't redocument
  `bin/send-message`, root-escalation, `memory/` layout, or how
  events route. That's all in boilerplate or kernel docs the PAI
  reads on demand.
- **No accumulated lessons.** Those belong in
  `memory/private/self.md` (surfaces as `<self-notes>` per nudge).

### Shape (in order)

1. **One sentence on identity.** "You are **email-pai** — the
   owner's email handler." Name the events it wakes on.
2. **Filesystem map.** 3–6 concrete paths it reads/writes most. The
   highest-leverage section — replaces an open-ended `rg` with a
   directed first look. Cap tight; universals are already covered.
3. **Per-event behavior.** One subsection per `wake_on:` entry. Each
   says: read X, decide between `{act, defer, surface to owner}`,
   call skill/bin Y. Concrete examples beat principles.
4. **Drafting / acting shape.** If the PAI writes artifacts (drafts,
   replies, files), show the literal yaml/format with comments.
5. **Style.** One paragraph. Register, terseness, what *not* to
   narrate.
6. **Memory.** One line: update when something significant comes up,
   check before non-trivial actions.
7. **Hard rules.** The handful of things that would burn a turn or
   harm the owner if the model guessed wrong. "Never click send."
   "Never commit on the owner's behalf." "One draft per inbound."

### Canonical examples

`pais/email-pai/prompt.md` and `pais/whatsapp-pai/prompt.md` are the
reference. Read both before writing a new prompt — they were just
tightened and embody the shape above.

### `boilerplate:` — selecting kernel-stitched blocks

Per-instance, in `/etc/config.yaml`:

```yaml
- name: email-pai
  prompt_dir: usr/lib/pais/email-pai
  boilerplate: [owner, memory-usage, capability-escalation]
```

Each name resolves to `etc/boilerplate/<name>.md`. Order preserved.
Missing → reconcile fails. Omit to take the kernel default.

### Multi-file `prompt_dir`

`prompt_dir` may point at a directory; every `*.md` is concatenated
in sorted order. Use for natural sections (`00-identity.md`,
`10-triage.md`, `20-rules.md`). A single `prompt.md` is also fine.

### Per-instance override

An instance can point its `prompt_dir` at
`var/lib/instances/<inst>/prompt/` for genuinely divergent role
text (e.g. work-email vs personal-email triage). Use sparingly —
most divergence belongs in instance memory, not a forked prompt.

## Ship flow

**Don't hand-write `package.yaml` + `prompt.md` from a blank file.** `paiman
init` scaffolds both with the right shape — start there and edit. Hand-writing
is how `version:`, `prompt:`, or `wake_on:` get forgotten and the bundle
silently no-ops.

**Pairegistry bundle** (general-purpose, the default):

```sh
cd ~/Projects/pairegistry/pais       # paiman init scaffolds into CWD's
paiman init <name>                   # pais/, hence the cd
$EDITOR pais/<name>/package.yaml pais/<name>/prompt.md

paiman install ~/Projects/pairegistry/pais/<name>   # install from local path
paiadd <name>                        # writes /etc/config.yaml entry,
                                     # creates instance dir,
                                     # emits kernel:reload_config
```

**Local-only bundle** (owner-specific, never going to pairegistry):

```sh
paiman init <name>                   # scaffolds /usr/lib/pais/<name>/
$EDITOR /usr/lib/pais/<name>/package.yaml prompt.md
paiadd <name>                        # no install step — already in place
```

**Lifecycle after `paiadd`:**

```sh
paictl stop <inst>                   # mark inactive
paictl start <inst>                  # reactivate
paidel <inst>                        # remove entry; preserves instance state
paidel <inst> --purge                # also wipe /var/lib/instances/<inst>/
```

All four end by emitting `kernel:reload_config`. Hand-edit
`/etc/config.yaml` only to fix an entry.

**Forgetting `paiadd` is the #1 silent failure.** `paiman install` only puts
the *template* in place — the kernel doesn't wake a bundle that has no fleet
entry. If `paictl status <name>` shows nothing after install, you skipped
`paiadd`.

## Don't

- Don't vendor drivers, skills, or bins in the bundle. Declare under
  `deps:`.
- Don't bake instance state (drafts, memory) into the bundle.
- Don't write a prompt that restates kernel boilerplate or kernel
  docs.
- Don't use the owner's name in prompt.md.
- Don't wake on a wildcard if a specific event works.
- For build/codegen sub-tasks, the PAI calls `execute-claudecode`,
  not a "coder" persub.

## Read these next

- `memory/doc/FILESYSTEM_v3.md` — bundle/instance/process layers.
- `memory/doc/KERNEL.md` — `/etc/config.yaml` + reconcile.
- `memory/doc/PAIMAN.md` — install/dep resolution.
- `pais/email-pai/`, `pais/whatsapp-pai/` — canonical references.
