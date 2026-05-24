---
name: drive-macos-ui
description: Drive a macOS app's UI when it has no CLI/API. Prefer the ax driver; osascript/System Events is the fallback. Decode AX errors correctly, dump the tree before acting, and never tell the owner you lack permission unless you actually saw a permission error.
---

# Driving a macOS app's UI

Use when the owner asks for something only reachable through a GUI app: toggle
a setting, configure local app state, or click through an app with no
`osascript` dictionary. Not this skill: anything with a CLI/API/URL-scheme —
use that instead, it's faster and doesn't fight accessibility.

## Reach for `ax` first

The `ax` driver is the purpose-built tool: it returns a **compressed,
ref-numbered actionable tree** and actuates by ref. It beats hand-rolled
`osascript` on every axis — one round trip to see the controls, stable refs,
real press/set_value. Check it's installed:

```sh
ax list_sessions 2>&1   # "axd not built…" → run: paiman install drivers/ax
                        # "socket not present…" → run: paictl start ax-in
```

If `ax` is live, the loop is:

```sh
ax attach <bundle_id> --show-owner          # -> session_id + tree
ax act <sid> <ref> press                    # or set_value/show_menu/pick_menu_item
ax redump <sid>                             # verify before the next dependent act
ax detach <sid>
```

**`--show-owner` is required for owner-initiated tasks in a visible app.**
Without it, `ax act` refuses to touch the frontmost window (background-piloting
default) and returns `EFOREGROUND`. The owner asked you to act in the window
they're looking at, so waive the gate. (Secure-input fields — passwords, sudo —
stay blocked regardless.)

`expand <ref>` drills into a node; `show_menu` / `pick_menu_item --title`
handle dropdowns. Re-`attach` returns `EDUPSCOPE` while the session is live.

## Verification contract

`ax act` success means the Accessibility call was accepted, not that the
owner's requested app state exists. Before each state-changing action, name the
observable postcondition you need from a fresh readback. After the action, call
`ax redump <sid>` or use another app readback, then compare that postcondition
before taking the next dependent step.

This is especially important before committing actions such as Start, Save,
Send, Delete, Buy, or Submit. Do not press the committing control until the
requested state is visible in the current tree/readback. Verifying that a
button changed to Pause, Done, or Sent only proves the commit happened; it does
not prove the committed values were correct.

For editable controls, verify the resulting value/label, not just `{"ok":
true}`. For presses that open UI, verify the new sheet, field, row, or control
appeared. If the needed state is not observable through the current tree, use a
different readback path or report that verification is blocked instead of
claiming completion.

Screenshots are valid visual readback when the AX tree omits the relevant
state or renders it ambiguously. Use them to confirm the requested visible state
before detaching/quitting the AX session, so you can still redump or retry while
the app context is live. Prefer structured readback when it is sufficient;
screenshots complement it, they do not turn `ok: true` into verification.

Time fields surface as `AXDateTimeArea`; set them by ref:
`ax act <sid> <ref> set_value --value "7:50 AM"`. No keyboard fumbling.

## Fallback: osascript / System Events

When `ax` isn't installed, drive `System Events`. The same verification
contract applies: inspect, act, then read back before the next dependent step.

### 1. Dump the whole tree first — don't guess paths

```applescript
tell application "<App>" to activate
delay 0.4
tell application "System Events" to tell process "<App>"
  set everything to entire contents of window 1
  repeat with e in everything
    try
      log (role of e) & " | desc=" & (description of e) & " | value=" & (value of e)
    end try
  end repeat
end tell
```

`entire contents` is the reliable traversal. `every button of window 1` and
`first X whose description is …` **fail on nested elements** — that's what the
`-1719 Invalid index` error means, not "no such control." Read the dump, find
the real role+description, then target it.

### 2. Via `osascript`, `click` on nested elements often no-ops — use the keyboard

When you're in the `osascript` fallback (no `ax`), `click`-ing a control
found by `entire contents` sometimes does nothing on Catalyst/SwiftUI apps:
no error, no effect. (Through the `ax` driver, `press` actuates these same
controls fine — prefer it, and `redump` after to confirm rather than assuming
a no-op.) **Don't keep re-clicking.** If a
System Events `click` doesn't take, pivot to keyboard activation:

```applescript
tell application "System Events" to tell process "<App>"
  keystroke tab using {option down, command down}   -- focus the toolbar
  repeat 10 times
    keystroke tab                                    -- walk to the control
    delay 0.1
  end repeat
  keystroke space                                    -- activate it
end tell
```

Then type into the focused field: `keystroke "a" using command down` (select
all) → `keystroke "0650"` → `keystroke return`.

### 3. `exit 0` ≠ success — re-read the tree to verify

`osascript` exit 0 means the script *ran*, not that the UI *changed*. After
every action, dump the tree again and confirm the new state (a sheet appeared,
a value changed) before reporting done.

## Decode AX errors correctly — and don't cry "no permission"

| Error | Means | Do |
|---|---|---|
| `-1728` "Can't get …" | wrong/nonexistent element path | dump `entire contents`, retarget |
| `-1719` "Invalid index" | element is nested, not a direct child | traverse via `entire contents` |
| `1002` "not allowed to send keystrokes" | **real** permission denial | this is the only "no access" signal |
| `-25211` / `errAXNotImplemented` | TCC Accessibility not granted | check System Settings → Accessibility |

**Only `1002` / `-25211` / an explicit TCC error mean you lack permission.**
`-1728` and `-1719` are *your query being wrong*. Never tell the owner "I hit
accessibility walls / need permission" off the back of a `-1728` — that's a
false report. Confirm the element exists in the tree dump first.

## Don't waste turns on dead ends

- `find ~/Library …` unscoped will time out — scope to a known container.
- Synthesizing a `.shortcut` plist and `shortcuts import`/`run`-ing it doesn't
  work headlessly (import needs GUI approval; there's no `import` subcommand).
- A `paicron` job that `echo`s text is **not an alarm** — no sound, no
  notification. Don't pass it off as one.
