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
  Methods: `attach`, `detach`, `act`, `expand`, `redump`, `list_sessions`.
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

After an action changes the UI (a sheet opens, a value updates), call
`redump(session_id)` to get the fresh tree — re-`attach` is refused with
`EDUPSCOPE` while a session is live, and `expand` only drills a ref you
already hold.

Date/time pickers surface as `AXDateTimeArea` (also `AXDateField` /
`AXTimeField`). Their `value` is rendered as an ISO-8601 string in the
**local** time zone (e.g. `2000-01-01T07:50:00-08:00`); the H:M shown
there is the picker's on-screen time. Set them with
`act <ref> set_value --value "7:50 AM"` (also accepts `07:50`, `0750`,
`19:50`) — the driver writes a CFDate (in local time, the convention the
picker uses), not a string.

## Foreground gate

By default `act` refuses to actuate the **frontmost window of the
frontmost app** — that's the owner's active context, not background
automation. A scoped window coming to foreground emits
`ax:scope_lost {reason: "paused"}`; going back to background emits
`{reason: "resumed"}`. Observation continues across pause; only
actuation is gated.

**Override: `attach --show-owner`.** Owner-initiated tasks in a visible
app (e.g. "set an alarm in Clock") *want* PAI to drive the window the
owner is looking at. Attaching with `--show-owner` waives the foreground
gate for that session and suppresses the paused/resumed `scope_lost`
chatter. Secure-input gating (passwords, sudo, 1Password) still applies
regardless. Without `--show-owner`, foreground `act` returns
`EFOREGROUND`.

A `--show-owner` session also **raises the scoped app to the front before
each `act`**. This is load-bearing, not cosmetic: many controls (Clock's
"Add an alarm", any sheet/modal-spawning button) only present their sheet
when their app is the *active* app — `AXPress` on a backgrounded app
returns success but silently no-ops, and the sheet never appears. A PAI
lives in a terminal, so the app it's driving is almost never frontmost on
its own. The raise is idempotent (skipped when already active, no
flicker) and scoped to `--show-owner` only; background piloting
(`allowForeground=false`) keeps its no-focus-steal contract.

## Owner privacy

With no session attached, axd does not observe the owner. There is no
per-keystroke event log. The catch-all PAI never sees AX events unless
it has explicitly attached. This is the substrate for piloting, not
surveillance.

## Install

```
paiman install drivers/ax
```

Use the typed `drivers/ax` form: the driver and its `ax` bin client share
the name `ax`, so a bare `paiman install ax` resolves to the bin alone.
Installing the driver pulls the bin in via `deps:`.

Copies the driver bundle to `~/.pai/opt/paiman/driver/ax/`, symlinks
`~/.pai/usr/lib/drivers/ax/`, runs `sidecar/build.sh` to build and stage
`axd` at `~/.pai/usr/libexec/ax/axd`, and (via deps) installs the `ax`
bin tool to `~/.pai/usr/bin/ax` (staged at `~/.pai/opt/paiman/bin/ax/`).

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

Owner-initiated task in a visible app — set a Clock alarm:

```bash
ax attach com.apple.clock --show-owner   # waive the foreground gate
# press the "Alarms" radio, then "Add an alarm" (redump between steps),
# then set the time and save:
ax act s1 <alarms-ref> press
ax act s1 <add-ref> press
ax redump s1                             # the sheet's AXDateTimeArea now appears
ax act s1 <datetime-ref> set_value --value "6:50 AM"
ax act s1 <save-ref> press
ax detach s1
```

## Disable

```
paictl stop ax-in
```

Sidecar terminates cleanly within ~5s; every live session is torn down.

## Out of scope (v1)

- App-typed schemas (Spotify/Mail domain models on top of the generic tree).
- CGEvent keystroke synthesis for fields that don't respond to `AXSetValue`.
- Persistence across kernel reboots.
- Coalescing / rate caps — without an ambient firehose there's nothing
  to cap.
