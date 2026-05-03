# pairegistry

Source-of-truth registry for PAI userspace packages. `paiman` installs from here into the FHS runtime at `$PAI_ROOT` (default `~/.pai`).

This is being seeded as a 1:1 mirror of `pai/src/` (kernel repo). Once `paiman` is wired up, pairegistry becomes the source of truth and `pai/src/` keeps only the kernel (`boot/`).

## Layout

```
pairegistry/
  drivers/<name>/         # event-driven drivers (kind: driver)
  bin/<name>/             # PAI-callable tools (kind: bin)
  sbin/<name>/            # owner-only system tools (kind: sbin)
  skills/<name>/          # SKILL.md is the manifest — no package.yaml
  prompts/<name>.md       # flat prompt files — no package.yaml
  pais/<name>/            # PAI bundles (kind: pai)
  lib/<name>/             # shared libraries (kind: lib)
```

## Manifest convention

Every installable unit has a `package.yaml` at its root — uniformly, no exemptions:

```yaml
name: <slug>
kind: driver | bin | sbin | pai | lib | skill | prompt
version: 0.1.0
description: <one line>
entrypoint: <file>     # bin/sbin/prompt only
deps: [<slug>, ...]    # other registry packages required
# pai-only:
provider: <name>
model: <id>
prompt: <file>
wake_on: [event:type, ...]
```

**Skills** still ship `SKILL.md` as their runtime body (read by the agent at use-time), but `package.yaml` next to it is what paiman reads to install the skill.

**Prompts** are dir-wrapped: `prompts/<name>/{package.yaml, prompt.md}`. `entrypoint:` resolves to `prompt.md` inside the bundle dir.

## FHS install targets

| Registry kind | Installs to |
|---|---|
| `driver` | `/usr/lib/drivers/<name>/` |
| `bin` | `/usr/bin/<name>` (shim) + module on path |
| `sbin` | `/sbin/<name>` (shim) + module on path |
| `pai` | `/opt/<name>/<version>/` |
| `lib` | `/usr/lib/<name>/` |
| skill (SKILL.md) | `/usr/lib/skills/<name>/` |
| prompt (.md) | `/usr/share/prompts/<name>.md` |

## Nested driver namespaces

Some drivers ship sub-drivers in a nested namespace (e.g. `drivers.email.macmail` in pai code). The nested form is preserved in registry: `drivers/email/macmail/` is part of the `email` driver package, not its own package. The `email` driver's `package.yaml` is at `drivers/email/package.yaml`.

## Initial seed

This commit mirrors `pai@6c8995d`:

- `drivers/`: contacts, email (with nested macmail), imessage, messages
- `lib/tailer/`: shared cursor-tailer primitive used by drivers
- `bin/`: paictl, paiman, paiadd, paidel, paicron, paifs_init, ipc, subagent, ps, clear, compact, edit_file, addcontact, resolve_contact, imessage_backfill, mailsearch, pai
- `sbin/`: migrate, reboot, reset, tui
- `skills/`: 22 skills (author-driver, boot-sequence, etc.)
- `prompts/`: pai_default, root, subagent, subagent-persistent
- `pais/email/`: email PAI bundle

## Out of scope (still in pai repo)

- `boot/` — kernel image
- `usr/share/doc/` — kernel-internal architecture docs
