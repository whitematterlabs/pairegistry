---
name: author-pai-bundle
visible_to: [root]
description: Howto for creating a new PAI bundle — package.yaml, prompt, paiman init scaffolding, paiadd to instantiate. Reference when adding a new fleet member.
---

# Authoring a PAI bundle

**Stop — did you classify?** A PAI bundle is a new fleet member with
its own identity, waking on some driver's events. If you don't yet
have a driver for that surface, you're at Scope B (driver), not
Scope C (driver + bundle). Run `grow-capability` §"Step 2 — scope
triage" first; come back here only after the driver exists.

A PAI bundle is the **template** a PAI is instantiated from. Two
locations:

- `/opt/<pkg>/<ver>/` — release bundles (from `paiman install`).
- `/usr/lib/pais/<name>/` — **dev source**, edited in place;
  `paiadd` stitches directly from here, bypassing `/opt/`.

Bundle content is **immutable post-install**. Edits go to instance
state at `/var/lib/instances/<pai>/`.

## Where the source lives: pairegistry vs local

Same call as for drivers. **Pairegistry** (`~/Projects/pairegistry/pais/<name>/`)
when the bundle is general-purpose and would make sense on someone
else's PAI install — install via `paiman install <name>`. **Local**
(author `/usr/lib/pais/<name>/` directly) when the bundle is
owner-specific: a PAI tied to your particular drivers, your
contacts, your workflow. PAI is self-healing and autonomous; local
bundles are expected and fine. Just don't keep both copies of the
same name — pick one origin and stay there.

## Layout

```
/usr/lib/pais/<name>/
├── package.yaml      manifest
└── prompt.md         role prompt
```

That's the whole bundle in v1. Heavier pieces (drivers, skills) are
**system-shared**, not vendored — declared in `package.yaml`,
resolved by `paiman`, installed once at
`/usr/lib/drivers/<name>/` and `memory/skills/<name>/`.

## package.yaml

```yaml
name: scheduler-pai
version: 0.1.0
description: Schedules and triages calendar events.
default_instance: scheduler

required_drivers:
  - name: gcal
    version: ">=1.0"

required_skills:
  - reload-config

requested_capabilities:
  - read: /var/lib/memory/people
  - write: /var/lib/instances/scheduler

# optional baseline overrides; these become the prompt/provider/model
# the new instance gets at paiadd time
defaults:
  provider: deepseek
  model: deepseek-v4-pro
  wake_on:
    - gcal:*

# optional: declare which symlinks the kernel stitches into the
# instance's home. If omitted, the instance gets only the universal
# seeds (bin, inbox, workspace, memory/*, tmp) — no `communication/`,
# no per-channel views.
home:
  links:
    - link: calendar               # name under $HOME
      target: var/spool/communication/gcal   # path under PAI_ROOT
```

### `home.links`

A bundle is the right place to declare which slice of the filesystem its
PAI sees. By default the kernel stitches only the universals; channel
views (`mail/`, `drafts/`, `messages/`, …) come from the bundle.

Rules:
- `link` is a path under `$HOME`. Cannot collide with reserved seeds
  (`bin`, `inbox`, `workspace`, `memory`, `tmp`) — collision is a hard
  stitch error.
- `target` is interpreted relative to `PAI_ROOT` and must stay inside it
  (escape attempts via `..` are rejected at stitch time).
- One link per channel surface — narrow is better. An email-pai gets
  `mail/` and `drafts/`, not `communication/`. The point is *isolation*:
  email-pai shouldn't see iMessage, and vice versa.
- Bundleless PAIs (the seed `root` and pid-2 `pai`) get the broad
  `communication/` view from the kernel; only bundle-instantiated PAIs
  use `home.links`.

## prompt.md

The role prompt for this PAI. Same shape as
`/usr/share/prompts/pai_default.md`. Keep it minimal — accumulated
guidance belongs in the instance's `memory/private/`, not the
prompt.

## Scaffolding flow

```sh
paiman init <name>            # creates /usr/lib/pais/<name>/ skeleton
$EDITOR /usr/lib/pais/<name>/package.yaml prompt.md

paiadd <bundle>               # useradd-style wizard:
                              #   - asks for instance name (default from manifest)
                              #   - assigns a pid
                              #   - writes /etc/config.yaml entry
                              #   - creates /var/lib/instances/<name>/
                              #   - emits kernel:reload_config

# Lifecycle (after instantiation):
paictl stop <name>            # mark inactive (active: false on spec)
paictl start <name>           # re-activate
paidel <name>                 # remove fleet entry; preserves instance state
paidel <name> --purge         # also wipe /var/lib/instances/<name>/
```

All three of `paiadd`/`paidel`/`paictl start|stop` end by emitting
`kernel:reload_config`. **Hand-edit `/etc/config.yaml` only to fix
an entry** — adds and removes go through these tools.

## Persubs

If your new PAI needs a long-lived specialist child (memory
curator, GUI delegate), declare it under `dependencies:` in the
config entry — not as a separate bundle. See skill
`understand-persubs`.

## Don't

- Don't vendor drivers or skills inside the bundle. Declare them.
- Don't bake instance-specific state into the bundle. The bundle is
  the template; the instance is the configured copy.
- Don't write a prompt that duplicates `memory/doc/` material.
  The PAI can read docs at runtime via skills.

## Read these next

- `memory/doc/FILESYSTEM_v3.md` §"Bundle anatomy"
- Skill `understand-bundles-and-instances` — the trinity.
- Skill `kernel-tools` — paiman/paiadd/paidel/paictl/paicron.
- Skill `understand-config-reconcile` — what the wizard writes.
