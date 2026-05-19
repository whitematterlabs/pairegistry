# ax driver

Piloting surface for macOS Accessibility. PAIs attach a **session** scoped
to one `(pid, window_id)` and actuate that window's interactive elements
in the background — Spotify-PAI picks a song while Twitter-PAI scrolls
feed, neither sees the other or the owner.

The driver is *not* an ambient sensor. There is no system-wide event
firehose, no NDJSON event log, no per-keystroke wakeups on the catch-all
PAI. Sessions are explicit, scoped, in-memory, and forbid duplicate
scopes.

## Components

```
ax/
  package.yaml          # paiman driver manifest + sidecar build hook
  events.yaml           # 4 event kinds (scope_attached, tree_changed, action_result, scope_lost)
  inbound.py            # supervises axd; parses NDJSON; forwards to kernel bus with target_pid
  sidecar/
    Package.swift       # SPM, depends on AXSwift
    Sources/AXSidecar/  # Swift sources — axd binary
    build.sh            # idempotent build + stage to $PAI_ROOT/usr/libexec/ax/axd
```

The shell-callable `ax` RPC client lives in a sibling bin bundle at
`~/Projects/pairegistry/bin/ax/`. The driver pulls it in via `deps:` so
`paiman install ax` lands both.

## Architecture

```
┌─────────┐  shell    ┌──────────┐  unix sock  ┌──────┐
│  PAI    │ ────────> │ ax (bin) │ ──────────> │ axd  │ (Swift)
└─────────┘           └──────────┘  JSON-RPC   └──┬───┘
     ▲                                            │ stdout NDJSON
     │ nudge                                      ▼
┌────┴──────────┐                          ┌────────────┐
│ kernel router │ <─── emit_event ──────── │ inbound.py │
│ (target_pid)  │  (target_pid)            │ (supervisor)│
└───────────────┘                          └────────────┘
```

- **PAI → axd** runs through `bin/ax`, a thin client that opens
  `$PAI_ROOT/var/run/ax/axd.sock` and speaks line-delimited JSON-RPC.
  Methods: `attach`, `detach`, `act`, `expand`, `list_sessions`.
- **axd → PAI** goes through this driver. axd writes NDJSON to stdout
  with `target_pid` on every event; `inbound.py` calls
  `P.emit_event(payload, target_pid=...)`. The kernel router (modified
  in `src/boot/routing.py`) honors `target_pid` and bypasses `wake_on`
  matching, delivering the nudge point-to-point.

## Sessions

One axd, many concurrent sessions. Each session is scoped to exactly one
`(pid, window_id)`. The reverse index `{(pid, window_id) → session_id}`
enforces uniqueness — a second `attach` to the same scope returns
`EDUPSCOPE`.

State is in-memory in axd. There is no persistence: kernel is always-on,
and a sidecar restart is equivalent to revoking every session. Lost
sessions surface as `ax:scope_lost`.

## The tree

`attach` returns a **compressed actionable surface**, not the raw AX
tree. Non-interactive containers are folded out; only interactive roles
survive (`Button`, `TextField`, `Link`, `MenuItem`, `Checkbox`,
`RadioButton`, `ComboBox`, `Slider`, plus `StaticText` when it labels a
control). Each entry is `{ref, role, label, value, enabled}`. Refs are
sequential integers allocated per-session and tracked in the session's
ref table.

Use `expand(ref)` to drill into a node's children. Use `act(ref, ...)`
to fire `press`, `set_value`, `show_menu`, or `pick_menu_item`.

## Foreground gate

By default `act` refuses to actuate the **frontmost window of the
frontmost app** — that's the owner's active context, not background
automation. A scoped window coming to foreground emits
`ax:scope_lost {reason: "paused"}`; going back to background emits
`{reason: "resumed"}`. Observation continues across pause; only
actuation is gated. (A future `show_owner=true` attach flag will permit
foreground action; deferred for v1.)

## Owner privacy

With no session attached, axd does not observe the owner. There is no
per-keystroke event log. The catch-all PAI never sees AX events unless
it has explicitly attached. This is the substrate for piloting, not
surveillance.

## Install

```
paiman install ax
```

Copies the driver bundle to `~/.pai/opt/paiman/ax/`, symlinks
`~/.pai/usr/lib/drivers/ax/`, runs `sidecar/build.sh` to build and stage
`axd` at `~/.pai/usr/libexec/ax/axd`, and (via deps) installs the `ax`
bin tool to `~/.pai/usr/bin/ax`.

## Accessibility grant (required)

`axd` needs the macOS Accessibility TCC grant. On first launch it will
either prompt automatically or exit with rc=78. To grant manually:

1. System Settings → Privacy & Security → Accessibility.
2. Click `+`, navigate to `~/.pai/usr/libexec/ax/axd`, add it, enable.
3. `paictl restart ax-in`.

Without the grant the sidecar exits cleanly; the supervisor retries
every 60s without crash-looping. Check status via
`paictl status ax-in` and `~/.pai/sys/drivers/ax/sidecar.stderr.log`.

## Quick taste

```bash
# In a PAI shell:
ax attach com.apple.calculator
# => {"session_id":"s1","tree":[{"ref":1,"role":"Button","label":"7",...}, ...]}

ax act s1 press 1
ax act s1 press 17     # the "+" button
ax act s1 press 4      # "3"
ax act s1 press 23     # "="
# Calculator now reads "10". Each act emits an ax:action_result nudge
# to the calling PAI only.

ax list_sessions
ax detach s1
```

## Disable

```
paictl stop ax-in
```

Sidecar terminates cleanly within ~5s; every live session is torn down.

## Out of scope (v1)

- `show_owner=true` foreground-actuation flag.
- App-typed schemas (Spotify/Mail domain models on top of the generic tree).
- CGEvent keystroke synthesis for fields that don't respond to `AXSetValue`.
- Persistence across kernel reboots.
- Coalescing / rate caps — without an ambient firehose there's nothing
  to cap.
