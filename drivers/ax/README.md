# ax driver

Phase 1 — sensor only. A Swift sidecar (`axd`) fans in `AXObserver`
notifications across every running macOS app and emits NDJSON events.
The Python driver supervises the sidecar and forwards events onto the
kernel bus.

No actuation, no RPC surface, no ax-pilot persub yet (those are Phases 2
and 3 in `~/Projects/pai/AX_PLAN.md`).

## Layout

```
ax/
  package.yaml           # paiman manifest + install hook (builds + stages axd)
  events.yaml            # 14 event kinds the sidecar emits (see AX_PLAN §Outbound)
  inbound.py             # supervises axd, parses NDJSON, calls P.emit_event
  sidecar/
    Package.swift        # SPM, depends on AXSwift
    Sources/AXSidecar/   # Swift sources
    .build/release/axd   # built binary (checked-in prebuilt acceptable; ad-hoc signed)
    build.sh             # idempotent build + stage to $PAI_ROOT/usr/libexec/ax/axd
```

## Install

```
paiman install ax
```

This copies the bundle to `~/.pai/opt/paiman/ax/`, symlinks
`~/.pai/usr/lib/drivers/ax/` at it, and runs `sidecar/build.sh` as the
install hook. The build script:

1. If `sidecar/.build/release/axd` is missing or older than any source
   file under `Sources/`, runs `swift build -c release` (requires Xcode
   command line tools).
2. Codesigns the binary ad-hoc so the OS can grant it Accessibility.
3. Copies it to `$PAI_ROOT/usr/libexec/ax/axd`.

## Accessibility grant (required)

`axd` needs the macOS Accessibility TCC grant. On first launch it will
either prompt automatically (if the calling context is allowed to ask) or
exit with rc=78. To grant manually:

1. System Settings → Privacy & Security → Accessibility.
2. Click `+`, navigate to `~/.pai/usr/libexec/ax/axd`, add it, enable.
3. `paictl restart ax-in`.

Without the grant the sidecar exits cleanly; the driver retries every 60s
without crash-looping.

Phase 1 does **not** need Input Monitoring or per-app Automation grants
— those are required only for the Phase 2 actuation RPC.

## Verify

After install + grant:

```
# Watch the kernel-bus side:
tail -f ~/.pai/var/log/ax/events.ndjson

# Trigger events to look for:
#   - Switch apps → ax:focus_changed, ax:app_launched, ax:window_changed
#   - Type in a text field → ax:value_changed (rate-capped at 20/sec)
#   - Open a URL in Safari/Chrome → ax:url_changed
#   - System banner (Calendar reminder, AirDrop) → ax:notification
#   - Gmail in an open Chrome tab "Message sent" toast → ax:live_region
#   - Mail.app sending a message → ax:announcement
```

## Disable

```
paictl stop ax-in
```

Sidecar terminates cleanly within ~5s.

## Phase 2 / 3 (not built)

- RPC surface (`dump_focused_window`, `press`, `set_value`, …) over stdin.
- Element ref table with stable-identity re-resolution.
- Tree compression rules.
- `ax-pilot` persub in `~/Projects/pairegistry/pais/ax-pilot/`.
- Per-bundle-id `AXEnhancedUserInterface` allow/deny list.

See `~/Projects/pai/AX_PLAN.md` for the full design.
