---
name: manage-dependencies
visible_to: [root]
description: Use when installing, removing, or searching paiman bundles where bundle-to-bundle `deps:` come into play — what gets pulled in automatically, what blocks an uninstall, when something falls through to pip. For the full paiman command surface, see `kernel-tools`; this skill is only about deps.
---

# Manage bundle dependencies

Bundles declare their dependencies in `deps:` (a flat list of bare
names) in `package.yaml`. `paiman` resolves them registry-first; misses
fall through to pip. There are **no version pins** — the registry is a
single git tree at HEAD.

For the `paiman` command cheatsheet (`install`/`remove`/`search`/`list`/
`show`), see the `kernel-tools` skill. This skill only covers the
dependency-resolution behavior layered on top.

## How `deps:` is declared

```yaml
# pairegistry/pais/email-pai/package.yaml
name: email-pai
kind: pai
deps:
  - email          # registry bundle → drivers/email/
  - mailsearch     # registry bundle → bin/mailsearch/
```

- Flat list of bare names. No version, no extras, no constraint syntax.
- Entries must be strings; non-string entries fail the install.
- Cycles are detected and rejected.
- Honored on `kind: pai`, `subagent`, `skill`, and any primitive
  (`bin`, `sbin`, `driver`, `lib`, `prompt`).

## What `paiman install` resolves automatically

For each entry in `deps:`:

1. If a bundle with that name is already installed, skip.
2. Else look it up in the registry (walks every typed root:
   `drivers/`, `bin/`, `sbin/`, `lib/`, `skills/`, `prompts/`, `pais/`,
   `subagents/`, plus `skills/<topic>/<name>/`). If found, recursively
   install it.
3. Else queue it as a pip package. After all bundle installs finish,
   pip deps are batch-installed into the kernel venv at
   `/usr/lib/venv/` via `uv pip install --python <venv-python>`.

So `sbin/paiman install email-pai` pulls `email` (driver) and
`mailsearch` (bin), then `email` pulls `tailer`, then any unresolved
names get pip-installed in one shot.

Disambiguate same-named bundles across typed roots with
`<kind>/<name>` (e.g. `bin/subagent` vs `prompts/subagent`).

## What blocks an uninstall

```sh
sbin/paiman remove <name>
```

Refused if any installed `pai` / `subagent` / `skill` bundle lists
`<name>` in its `deps:`. The error names the dependents.

```sh
sbin/paiman remove <name> --force   # override; leaves dependents broken
```

Primitives (`bin`, `driver`, `lib`, `prompt`, `sbin`) depending on
`<name>` do **not** block — only pai/subagent/skill dependents do. Pip
deps are never uninstalled.

## Inspect

```sh
sbin/paiman show <name> | grep -A5 '^deps:'        # what a bundle pulls in
grep -rl "^- <name>$" /opt/paiman/*/package.yaml   # who depends on <name>
```

`paiman list` shows kind+version only — no dep tree view; grep
`/opt/paiman/` if you need one.

## Authority

- Resolver: `_install_from_source`, `_Registry.lookup` in
  `/usr/src/bin/paiman.py`.
- Uninstall guard: `_bundles_depending_on` in the same file.
- Full paiman reference: `memory/doc/PAIMAN.md`.
- Filesystem layout the activation slots write to:
  `memory/doc/FILESYSTEM_v3.md`.
