---
name: manage-subagent-bundles
visible_to: [root]
description: Use when authoring or curating reusable `kind: subagent` bundles at /usr/lib/subagents/ (e.g. scout, browse). For wiring an existing bundle into a parent, see manage-dependencies; for coding work use execute-claudecode instead.
---

# Manage subagent bundles

A **subagent bundle** is a reusable role template — role prompt + optional provider/model defaults — at `/usr/lib/subagents/<name>/`, installed from `~/Projects/pairegistry/subagents/<name>/`. Any parent PAI can pull one in via `dependencies: [{name: ..., package: <bundle>}]`, or spawn ad-hoc with `bin/subagent spawn --package <bundle> --slug ... --prompt "..."`.

Authority: `memory/doc/SUBAGENT_BUNDLES.md` (layout, resolution chain, lifecycle). This skill is just the authoring workflow — don't reread the doc back in here.

## Not the right tool

- **Coding tasks.** Do not author a `coder` bundle. The coder bundle is deprecated; coding work goes through the `execute-claudecode` skill, which shells out to `claude -p` directly. No subagent involved.
- **One-shot lookups under a single parent.** Skip the bundle, just `bin/subagent spawn --slug X --prompt "..."` (ephemeral) or inline `dependencies:` (no `package:`).
- **You haven't picked provider/model.** Either commit to both or omit both — see resolution chain in `SUBAGENT_BUNDLES.md`. Don't ship a half-bundle.

Author a bundle when the role is reusable across parents AND the prompt is non-trivial — `scout` (read-only investigation) and `browse` (browser-use driver) are the live examples.

## Bundle shape

Look at the real ones first:

```
~/Projects/pairegistry/subagents/scout/
├── package.yaml      # name, kind: subagent, description, prompt, provider
└── prompt.md

~/Projects/pairegistry/subagents/browse/
├── package.yaml      # provider/model omitted → cascade to parent
├── prompt.md
├── entry.py          # optional: custom spawn entrypoint
└── libexec/          # optional: install.sh etc.
```

Minimum `package.yaml`:

```yaml
name: <name>
kind: subagent
version: 0.1.0
description: <one-line catalog blurb; shows in `paiman list`>
prompt: usr/lib/subagents/<name>/prompt.md
# provider: anthropic         # optional — set both or neither
# model: claude-sonnet-4-5
# libexec:                    # optional — install hook etc.
#   install: ["bash", "install.sh"]
```

`prompt.md` is the role prompt prepended to the persistent-subagent system block on every turn. Keep it about who-the-role-is and operating principles. Do not put owner-specific names in it ("the owner", never a first name).

## Authoring workflow

1. Edit in pairegistry, not in the installed tree:
   ```
   cd ~/Projects/pairegistry/subagents/
   cp -r scout <name>/        # crib from the closest existing bundle
   ```
   Or `paiman init <name> --type subagent` if you want the scaffolder to write the stub.
2. Fill in `package.yaml` and `prompt.md`. Rules: name has no `/`, `.`, or leading `-`; singular and role-shaped.
3. Install / refresh:
   ```
   paiman install <name>
   paiman show <name>         # verify resolved package.yaml
   ```
4. Smoke-test from a parent turn:
   ```
   bin/subagent spawn --package <name> --slug <name>-test --prompt "..."
   ```

## Iterating on a live bundle

Subagent specs are captured at spawn time — editing `package.yaml` / `prompt.md` does not retroactively patch a running subagent. Respawn it to pick up bundle changes.

## Inspect

```
paiman list                  # all bundles, grouped by type
paiman show <name>           # resolved package.yaml
ls /usr/lib/subagents/       # raw view
```
