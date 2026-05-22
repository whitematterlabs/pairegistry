# AX Driver: Surface, Read & Write Date/Time Pickers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `ax` driver able to drive macOS date/time pickers (e.g. set a Clock alarm) end-to-end, by surfacing `AXDateTimeArea` elements, reading their `CFDate` values, writing them back, and adding a `redump` RPC so a PAI can re-read the tree after an action mutates the UI.

**Architecture:** All changes are in the `axd` Swift sidecar (`~/Projects/pairegistry/drivers/ax/sidecar`) plus the `ax` Python client (`~/Projects/pairegistry/bin/ax/ax.py`) and docs. macOS already exposes the Clock alarm time as an `AXDateTimeArea` whose `AXValue` is a writable `CFDate` (proven live: `AXUIElementSetAttributeValue` → `AXError=0`). The driver was hiding it via a role allowlist, couldn't bridge a `CFDate` value, set `set_value` as a `CFString`, and had no way to re-read the tree after an action. This plan fixes all four.

**Tech Stack:** Swift 6.3 (SwiftPM, AXSwift, ApplicationServices/HIServices C API), Python 3 (stdlib), macOS Accessibility.

---

## Background: the four defects (all verified against live Clock)

| # | Defect | Location | Evidence |
|---|---|---|---|
| 1 | Role allowlist prunes the time field | `AppObserver.swift:17` `interactiveRoles` (no `AXDateTimeArea`); `TreeExtractor.swift:42` only emits interactive roles | Raw dump shows `AXDateTimeArea`; `ax` tree never does |
| 2 | No way to re-read the tree after an action | `RPCServer.swift:116` dispatch — only `attach/detach/act/expand/list_sessions`; re-`attach` → `EDUPSCOPE` | PAI went blind after the add-alarm sheet opened |
| 3 | Can't render a `CFDate` value | `AppObserver.swift:37` `rawStringAttr` bridges only String/URL/NSNumber/Bool | `AXDateTimeArea` value came back blank |
| 4 | `set_value` writes a `CFString` onto a `CFDate` attribute | `Actuator.swift:79` `performSetValue` | README's `set_value --value 0650` never worked; CFDate write proven to succeed |

## File structure

| File | Responsibility | Change |
|---|---|---|
| `drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift` | Static AX helpers (`interactiveRoles`, attribute bridging) | Add date/time roles; bridge `Date`; add `rawAttr` + `parseTimeOfDay` |
| `drivers/ax/sidecar/Sources/AXSidecar/Actuator.swift` | Perform actions on elements | `set_value` sets a `CFDate` when the target's value is a date |
| `drivers/ax/sidecar/Sources/AXSidecar/RPCServer.swift` | JSON-RPC dispatch + handlers | Add `redump` method + handler |
| `bin/ax/ax.py` | Shell-callable RPC client | Add `redump` subcommand |
| `drivers/ax/README.md` | Driver docs | Document `redump` + date-picker support |
| `skills/operating/drive-macos-ui/SKILL.md` | PAI-facing how-to | Correct the false "AXPress no-ops" lesson; document the date-area recipe |

## Conventions used by every task

**Deploy after editing sidecar Swift** (rebuilds, ad-hoc signs, stages `axd`, restarts the driver so the new binary is live):

```bash
cd /Users/arda/Projects/pairegistry/drivers/ax/sidecar && ./build.sh
/Users/arda/.pai/usr/bin/paictl restart ax-in
sleep 2   # let the supervisor respawn axd + rebind the socket
```

**Deploy after editing the client** `bin/ax/ax.py` (the installed shim execs the staged copy):

```bash
cp /Users/arda/Projects/pairegistry/bin/ax/ax.py /Users/arda/.pai/opt/paiman/bin/ax/ax.py
```

**Test harness.** There is no XCTest target (AX behavior needs a live app + the Accessibility grant, which only the host terminal has). Tests are live integration checks through the `ax` CLI. Every test block starts with:

```bash
export PAI_PID=$$                       # ax refuses to run without it
AX=/Users/arda/.pai/usr/bin/ax
```

**Open the add-alarm sheet** (known-good setup used by Tasks 3–5; `ax`-native flow is proven in Task 6):

```bash
osascript -e 'tell application "Clock" to activate' -e 'delay 0.5' -e '
tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXMenuButton" and description of e is "Add an alarm" then click e
    end try
  end repeat
end tell'
sleep 1
```

**Close/cancel the sheet + delete any test alarm** (cleanup at the end of a test):

```bash
osascript -e 'tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXButton" and title of e is "Cancel" then click e
    end try
  end repeat
end tell' 2>/dev/null
osascript -e 'tell application "System Events" to tell process "Clock" to key code 53' 2>/dev/null  # Esc
```

---

### Task 0: Branch

**Files:** none (git only)

- [ ] **Step 1: Create a feature branch in pairegistry**

```bash
cd /Users/arda/Projects/pairegistry
git checkout -b ax-datetime-pickers
```

- [ ] **Step 2: Confirm the branch**

Run: `git rev-parse --abbrev-ref HEAD`
Expected: `ax-datetime-pickers`

---

### Task 1: Add the `redump` RPC (keystone)

Lets a PAI re-read the current tree for a live session without re-attaching. This is what unblocks every multi-step flow.

**Files:**
- Modify: `drivers/ax/sidecar/Sources/AXSidecar/RPCServer.swift` (dispatch ~line 116; add handler after `handleExpand`, ~line 286)
- Modify: `bin/ax/ax.py` (add subcommand)

- [ ] **Step 1: Write the failing test (pre-fix, `redump` is unknown)**

```bash
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax
SID=$($AX attach com.apple.clock --show-owner | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
echo "session=$SID"
$AX redump "$SID"
$AX detach "$SID"
```

- [ ] **Step 2: Run it to verify it fails**

Run the Step 1 block.
Expected: the `redump` call fails — the client errors with `argument cmd: invalid choice: 'redump'` (client has no such subcommand yet). (If the client somehow forwards it, the server returns `{"ok": false, "error": "EMETHOD"}`.)

- [ ] **Step 3: Add the dispatch case in `RPCServer.swift`**

In `processLine`, add the `redump` case alongside the others (after the `expand` case, ~line 120):

```swift
        case "expand":        handleExpand(params, requestID: requestID, fd: fd)
        case "redump":        handleRedump(params, requestID: requestID, fd: fd)
        case "list_sessions": handleListSessions(params, requestID: requestID, fd: fd)
```

- [ ] **Step 4: Add the `handleRedump` handler in `RPCServer.swift`**

Insert immediately after `handleExpand` (after its closing brace, ~line 286):

```swift
    private func handleRedump(_ params: [String: Any], requestID: String, fd: Int32) {
        guard let sid = params["session_id"] as? String,
              let session = SessionManager.shared.session(id: sid) else {
            sendError(fd, requestID: requestID, code: "ENOSESSION", message: "")
            return
        }
        // Re-walk session.window from scratch. Picks up sheets/popovers that
        // appeared since attach. allocRef is monotonic, so this mints fresh
        // refs for the current elements; older refs keep resolving.
        let tree = TreeExtractor.dump(session: session)
        sendOK(fd, requestID: requestID,
               result: ["session_id": sid, "tree": tree])
    }
```

- [ ] **Step 5: Update the method list in the `RPCServer` doc comment**

Change the methods comment block (lines 8–13) to include `redump`:

```swift
///   expand(session_id, ref)
///   redump(session_id)                          // re-read the current tree
///   list_sessions(target_pid?)
```

- [ ] **Step 6: Add the `redump` subcommand to `bin/ax/ax.py`**

Add the handler after `cmd_expand` (~line 145):

```python
def cmd_redump(args: argparse.Namespace) -> None:
    _print_and_exit(_call("redump", {"session_id": args.session_id}))
```

Add the parser after the `expand` parser block (~line 188), before `list_sessions`:

```python
    rd = subs.add_parser("redump",
                         help="Re-read the current tree for a session "
                              "(after an action changed the UI).")
    rd.add_argument("session_id")
    rd.set_defaults(func=cmd_redump)
```

Update the module docstring (line 9 area) to list it:

```python
  ax expand <session_id> <ref>
  ax redump <session_id>
  ax list_sessions [--mine]
```

- [ ] **Step 7: Deploy sidecar + client**

```bash
cd /Users/arda/Projects/pairegistry/drivers/ax/sidecar && ./build.sh
/Users/arda/.pai/usr/bin/paictl restart ax-in
sleep 2
cp /Users/arda/Projects/pairegistry/bin/ax/ax.py /Users/arda/.pai/opt/paiman/bin/ax/ax.py
```

Expected: `ax/build.sh: staged → …/usr/libexec/ax/axd`.

- [ ] **Step 8: Run the test to verify it passes**

```bash
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax
SID=$($AX attach com.apple.clock --show-owner | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
$AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print('tree_len=',len(d['tree']));print('has_alarms_tab=', any(e.get('label')=='Alarms' for e in d['tree']))"
$AX detach "$SID"
```

Expected: `tree_len=` a positive number and `has_alarms_tab= True`.

- [ ] **Step 9: Commit**

```bash
cd /Users/arda/Projects/pairegistry
git add drivers/ax/sidecar/Sources/AXSidecar/RPCServer.swift bin/ax/ax.py
git commit -m "ax: add redump RPC so a PAI can re-read the tree after an action

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Surface `AXDateTimeArea` (and date/time fields) in the tree

**Files:**
- Modify: `drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift` (`interactiveRoles`, ~line 17)

- [ ] **Step 1: Write the failing test (pre-fix the date area is pruned)**

```bash
# setup: open the add-alarm sheet
osascript -e 'tell application "Clock" to activate' -e 'delay 0.5' -e '
tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXMenuButton" and description of e is "Add an alarm" then click e
    end try
  end repeat
end tell'
sleep 1
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax
SID=$($AX attach com.apple.clock --show-owner | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
$AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print('datetime_count=', sum(1 for e in d['tree'] if e['role']=='AXDateTimeArea'))"
$AX detach "$SID"
# cleanup
osascript -e 'tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXButton" and title of e is "Cancel" then click e
    end try
  end repeat
end tell' 2>/dev/null
```

- [ ] **Step 2: Run it to verify it fails**

Run the Step 1 block.
Expected: `datetime_count= 0` (the element exists in macOS's tree but `axd` prunes it).

- [ ] **Step 3: Add date/time roles to `interactiveRoles`**

In `AppObserver.swift`, extend the set (after the `AXSlider`/`AXIncrementor` line, ~line 24):

```swift
        "AXComboBox",
        "AXSlider", "AXIncrementor",
        "AXDateTimeArea", "AXDateField", "AXTimeField",
        "AXTabGroup",
        "AXDisclosureTriangle",
```

- [ ] **Step 4: Deploy sidecar**

```bash
cd /Users/arda/Projects/pairegistry/drivers/ax/sidecar && ./build.sh
/Users/arda/.pai/usr/bin/paictl restart ax-in
sleep 2
```

- [ ] **Step 5: Run the test to verify it passes**

Run the Step 1 block again.
Expected: `datetime_count= 1`.

- [ ] **Step 6: Commit**

```bash
cd /Users/arda/Projects/pairegistry
git add drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift
git commit -m "ax: surface AXDateTimeArea/AXDateField/AXTimeField in the tree

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Render a `CFDate` value as an ISO-8601 UTC string

So the surfaced `AXDateTimeArea` shows its current time instead of a blank value.

**Files:**
- Modify: `drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift` (`rawStringAttr`, ~line 37)

- [ ] **Step 1: Write the failing test (value is blank pre-fix)**

```bash
osascript -e 'tell application "Clock" to activate' -e 'delay 0.5' -e '
tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXMenuButton" and description of e is "Add an alarm" then click e
    end try
  end repeat
end tell'
sleep 1
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax
SID=$($AX attach com.apple.clock --show-owner | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
$AX redump "$SID" | python3 -c "import sys,json,re;d=json.load(sys.stdin);v=[e['value'] for e in d['tree'] if e['role']=='AXDateTimeArea'][0];print('value=',repr(v));print('is_time=', bool(re.search(r'T\d\d:\d\d', v)))"
$AX detach "$SID"
osascript -e 'tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXButton" and title of e is "Cancel" then click e
    end try
  end repeat
end tell' 2>/dev/null
```

- [ ] **Step 2: Run it to verify it fails**

Run the Step 1 block.
Expected: `value= ''` and `is_time= False`.

- [ ] **Step 3: Bridge `Date` in `rawStringAttr`**

In `AppObserver.swift`, add a `Date` branch before the final `return nil` (after the `Bool` branch, ~line 45):

```swift
        if let b = v as? Bool { return b ? "true" : "false" }
        if let d = v as? Date {
            let f = ISO8601DateFormatter()
            f.timeZone = TimeZone(identifier: "UTC")
            return f.string(from: d)
        }
        return nil
```

- [ ] **Step 4: Deploy sidecar**

```bash
cd /Users/arda/Projects/pairegistry/drivers/ax/sidecar && ./build.sh
/Users/arda/.pai/usr/bin/paictl restart ax-in
sleep 2
```

- [ ] **Step 5: Run the test to verify it passes**

Run the Step 1 block again.
Expected: `value=` something like `'2000-01-01T15:52:42Z'` and `is_time= True`.

- [ ] **Step 6: Commit**

```bash
cd /Users/arda/Projects/pairegistry
git add drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift
git commit -m "ax: render CFDate AXValue as an ISO-8601 UTC string

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Write a time via `set_value` (CFDate, not CFString)

**Files:**
- Modify: `drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift` (add `rawAttr` + `parseTimeOfDay` helpers)
- Modify: `drivers/ax/sidecar/Sources/AXSidecar/Actuator.swift` (`performSetValue`, ~line 71)

- [ ] **Step 1: Write the failing test (CFString set on a date attr fails / no-ops)**

```bash
osascript -e 'tell application "Clock" to activate' -e 'delay 0.5' -e '
tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXMenuButton" and description of e is "Add an alarm" then click e
    end try
  end repeat
end tell'
sleep 1
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax
SID=$($AX attach com.apple.clock --show-owner | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
REF=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXDateTimeArea'))")
echo "date-area ref=$REF"
$AX act "$SID" "$REF" set_value --value 07:50
$AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);v=[e['value'] for e in d['tree'] if e['role']=='AXDateTimeArea'][0];print('value=',v);print('is_0750=', 'T07:50' in v)"
$AX detach "$SID"
osascript -e 'tell application "System Events" to tell process "Clock"
  set xs to entire contents of window 1
  repeat with e in xs
    try
      if role of e is "AXButton" and title of e is "Cancel" then click e
    end try
  end repeat
end tell' 2>/dev/null
```

- [ ] **Step 2: Run it to verify it fails**

Run the Step 1 block.
Expected: the `set_value` call returns `{"ok": false, ...}` (or `ok:true` with no change), and `is_0750= False`.

- [ ] **Step 3: Add `rawAttr` and `parseTimeOfDay` to `AppObserver.swift`**

Add both inside `enum AXHelpers`, after `childrenAttr` (~line 69):

```swift
    /// Raw attribute value as the bridged Foundation object (no String
    /// coercion). Lets callers inspect the dynamic type — e.g. a date
    /// picker's AXValue is a Date, which set_value must set as a CFDate.
    static func rawAttr(_ element: UIElement, _ key: String) -> AnyObject? {
        var value: AnyObject?
        let err = AXUIElementCopyAttributeValue(
            element.element, key as CFString, &value)
        guard err == .success else { return nil }
        return value
    }

    /// Parse a wall-clock time ("07:50", "7:50 AM", "0750", "19:50") into a
    /// Date that keeps `ref`'s calendar day but replaces hour/minute,
    /// evaluated in UTC — the convention macOS date pickers use for AXValue
    /// (a UTC date whose H:M is what the picker displays). nil if unparseable
    /// or out of range.
    static func parseTimeOfDay(_ raw: String, basedOn ref: Date) -> Date? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        var pm = false, am = false
        if s.hasSuffix("AM") { am = true; s.removeLast(2) }
        else if s.hasSuffix("PM") { pm = true; s.removeLast(2) }
        s = s.trimmingCharacters(in: .whitespaces)

        var hour: Int? = nil
        var minute: Int? = nil
        if s.contains(":") {
            let parts = s.split(separator: ":", maxSplits: 1)
            if parts.count == 2 { hour = Int(parts[0]); minute = Int(parts[1]) }
        } else if let n = Int(s) {
            if s.count <= 2 { hour = n; minute = 0 }       // "7", "19"
            else { hour = n / 100; minute = n % 100 }       // "0750" -> 7,50
        }
        guard var h = hour, let m = minute, (0...59).contains(m) else { return nil }
        if pm && h < 12 { h += 12 }
        if am && h == 12 { h = 0 }
        guard (0...23).contains(h) else { return nil }

        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC")!
        var comp = cal.dateComponents([.year, .month, .day], from: ref)
        comp.hour = h; comp.minute = m; comp.second = 0
        return cal.date(from: comp)
    }
```

- [ ] **Step 4: Make `performSetValue` detect a date target in `Actuator.swift`**

Replace the body of `performSetValue` (lines 71–86) with:

```swift
    private static func performSetValue(_ element: UIElement,
                                        args: [String: Any]) -> Result<Void, ActError> {
        if SecureInputPoller.shared.isActive {
            return .failure(.secureInput)
        }
        guard let value = args["value"] as? String else {
            return .failure(.unsupportedAction("set_value:missing value"))
        }
        // Date/time pickers (e.g. Clock's alarm AXDateTimeArea) carry a
        // CFDate AXValue; setting a CFString silently fails or no-ops.
        // Detect by the current value's type and set a CFDate instead.
        if let curDate = AXHelpers.rawAttr(element, kAXValueAttribute as String) as? Date {
            guard let newDate = AXHelpers.parseTimeOfDay(value, basedOn: curDate) else {
                return .failure(.unsupportedAction("set_value:bad time \(value)"))
            }
            let err = AXUIElementSetAttributeValue(
                element.element, kAXValueAttribute as CFString, newDate as NSDate)
            if err == .success { return .success(()) }
            return .failure(.axError("setDate=\(err.rawValue)"))
        }
        let err = AXUIElementSetAttributeValue(
            element.element,
            kAXValueAttribute as CFString,
            value as CFString
        )
        if err == .success { return .success(()) }
        return .failure(.axError("setAttr=\(err.rawValue)"))
    }
```

- [ ] **Step 5: Deploy sidecar**

```bash
cd /Users/arda/Projects/pairegistry/drivers/ax/sidecar && ./build.sh
/Users/arda/.pai/usr/bin/paictl restart ax-in
sleep 2
```

- [ ] **Step 6: Run the test to verify it passes**

Run the Step 1 block again.
Expected: the `set_value` call returns `{"ok": true}` and `is_0750= True` (value contains `T07:50`).

- [ ] **Step 7: Commit**

```bash
cd /Users/arda/Projects/pairegistry
git add drivers/ax/sidecar/Sources/AXSidecar/AppObserver.swift drivers/ax/sidecar/Sources/AXSidecar/Actuator.swift
git commit -m "ax: set_value writes a CFDate to date/time pickers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: End-to-end — set a real Clock alarm purely through `ax`

Proves the whole flow works without osascript and leaves Clock clean.

**Files:** none (integration verification only)

- [ ] **Step 1: Run the full `ax`-native flow**

```bash
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax

# Make sure no leftover session / alarm from earlier runs
$AX list_sessions --mine | python3 -c "import sys,json;[print(s['session_id']) for s in json.load(sys.stdin).get('sessions',[])]" | while read s; do [ -n "$s" ] && $AX detach "$s"; done

SID=$($AX attach com.apple.clock --show-owner | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

# 1) switch to Alarms tab
ALARMS=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXRadioButton' and e.get('label')=='Alarms'))")
$AX act "$SID" "$ALARMS" press

# 2) open the add-alarm sheet
ADD=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXMenuButton' and e.get('label')=='Add an alarm'))")
$AX act "$SID" "$ADD" press

# 3) set the time
DT=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXDateTimeArea'))")
$AX act "$SID" "$DT" set_value --value "7:50 AM"

# 4) save
SAVE=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXButton' and e.get('label')=='Save'))")
$AX act "$SID" "$SAVE" press

# 5) verify an alarm button now reads 07:50
$AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);btns=[e['label'] for e in d['tree'] if e['role']=='AXButton' and 'Alarm' in (e.get('label') or '')];print('alarm_buttons=',btns);print('has_0750=', any('07:50' in b for b in btns))"
echo "SID=$SID"
```

- [ ] **Step 2: Confirm the result**

Expected: `has_0750= True` and `alarm_buttons=` contains something like `['07:50, Alarm, On']`. Every `act`/`redump` returned `ok:true`. No osascript was used.

- [ ] **Step 3: Clean up the test alarm**

```bash
export PAI_PID=$$
AX=/Users/arda/.pai/usr/bin/ax
# reopen the alarm to reveal the Delete button, then press it
ALARM=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXButton' and '07:50' in (e.get('label') or '')))")
$AX act "$SID" "$ALARM" press
DEL=$($AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(e['ref'] for e in d['tree'] if e['role']=='AXButton' and e.get('label')=='Delete'))")
$AX act "$SID" "$DEL" press
$AX redump "$SID" | python3 -c "import sys,json;d=json.load(sys.stdin);print('no_alarms=', any(e.get('value')=='No Alarms' or e.get('label')=='No Alarms' for e in d['tree']) or not any('Alarm,' in (e.get('label') or '') for e in d['tree']))"
$AX detach "$SID"
```

Expected: `no_alarms= True`. (If `Delete` isn't surfaced as an `AXButton`, fall back to the osascript cleanup snippet in "Conventions"; note the gap for a follow-up but it does not block this plan.)

- [ ] **Step 4: No commit** (verification only). If Step 3 revealed a missing `Delete`/edit affordance, record it as a known follow-up in the PR description rather than expanding scope here.

---

### Task 6: Update docs — README + drive-macos-ui skill

Correct the two false lessons the old behavior taught: that AXPress "silently no-ops on Catalyst" (it doesn't — actuation works; the PAI just couldn't *observe* the result without `redump`) and that the time picker isn't AX-exposed.

**Files:**
- Modify: `drivers/ax/README.md`
- Modify: `skills/operating/drive-macos-ui/SKILL.md`

- [ ] **Step 1: README — add `redump` to the methods list**

In `drivers/ax/README.md`, in the "PAI → axd" methods line (~line 46), change:

```
  Methods: `attach`, `detach`, `act`, `expand`, `list_sessions`.
```

to:

```
  Methods: `attach`, `detach`, `act`, `expand`, `redump`, `list_sessions`.
```

- [ ] **Step 2: README — document re-reading + date pickers in "The tree"**

In `drivers/ax/README.md`, after the "Use `expand(ref)` …" paragraph (~line 74), add:

```markdown

After an action changes the UI (a sheet opens, a value updates), call
`redump(session_id)` to get the fresh tree — re-`attach` is refused with
`EDUPSCOPE` while a session is live, and `expand` only drills a ref you
already hold.

Date/time pickers surface as `AXDateTimeArea` (also `AXDateField` /
`AXTimeField`). Their `value` is rendered as an ISO-8601 UTC string
(e.g. `2000-01-01T07:50:00Z`); the H:M shown there is the picker's
displayed time. Set them with `act <ref> set_value --value "7:50 AM"`
(also accepts `07:50`, `0750`, `19:50`) — the driver writes a CFDate, not
a string.
```

- [ ] **Step 3: README — fix the Clock example**

In `drivers/ax/README.md`, replace the `set_value --value 0650` example (~line 153) with the proven flow:

```bash
ax attach com.apple.clock --show-owner   # waive the foreground gate
# press the "Alarms" radio, then "Add an alarm" (redump between steps),
# then set the time and save:
ax act s1 <add-ref> press
ax redump s1                             # the sheet's AXDateTimeArea now appears
ax act s1 <datetime-ref> set_value --value "6:50 AM"
ax act s1 <save-ref> press
ax detach s1
```

- [ ] **Step 4: drive-macos-ui skill — correct the AXPress claim**

In `skills/operating/drive-macos-ui/SKILL.md`, under "Reach for `ax` first", append a paragraph after the `expand`/`show_menu` line:

```markdown

After any `press`/`set_value` that should change the UI, call `ax redump
<sid>` to see the result — don't conclude "the press no-oped" from a stale
tree. A press that returns `{"ok": true}` did fire; if nothing seems to
have changed, redump before retrying. Re-`attach` returns `EDUPSCOPE`
while the session is live.

Time fields surface as `AXDateTimeArea`; set them by ref:
`ax act <sid> <ref> set_value --value "7:50 AM"`. No keyboard fumbling.
```

- [ ] **Step 5: drive-macos-ui skill — soften the osascript "AXPress no-ops" rule**

In `skills/operating/drive-macos-ui/SKILL.md`, in section "2. AXPress often silently no-ops", change the opening sentence so it's scoped to the osascript fallback and not stated as a universal AX fact:

```markdown
### 2. Via `osascript`, `click` on nested elements often no-ops — use the keyboard

When you're in the `osascript` fallback (no `ax`), `click`-ing a control
found by `entire contents` sometimes does nothing on Catalyst/SwiftUI apps.
(Through the `ax` driver, `press` actuates these same controls fine — prefer
it.) If a System Events `click` doesn't take, pivot to keyboard activation:
```

- [ ] **Step 6: drive-macos-ui skill — fix the "Proven recipe" for Clock**

In `skills/operating/drive-macos-ui/SKILL.md`, replace the "With `ax` (preferred)" line under "Proven recipe — Clock alarm" with:

```markdown
With `ax` (preferred): `attach com.apple.clock --show-owner` → `press` the
`Alarms` radio → `press` `Add an alarm` → `redump` → `set_value` the
`AXDateTimeArea` ref (`--value "6:50 AM"`) → `press` `Save` → `detach`.
Re-`redump` after each step instead of guessing.
```

- [ ] **Step 7: Deploy the skill change (so the running PAI sees it)**

The skill is symlinked into the runtime via paiman; confirm and refresh if needed:

```bash
ls -l /Users/arda/.pai/usr/lib/skills/operating/drive-macos-ui/SKILL.md
# if it's a symlink into pairegistry, the edit is already live. If it's a copy:
#   paiman install skills/operating/drive-macos-ui
```

Expected: the path resolves to the edited file (symlink into `~/Projects/pairegistry/...`) or you reinstall.

- [ ] **Step 8: Commit**

```bash
cd /Users/arda/Projects/pairegistry
git add drivers/ax/README.md skills/operating/drive-macos-ui/SKILL.md
git commit -m "docs(ax): document redump + date pickers; correct AXPress no-op myth

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:**
- Defect 1 (pruned date field) → Task 2 ✅
- Defect 2 (no re-read) → Task 1 ✅
- Defect 3 (CFDate unreadable) → Task 3 ✅
- Defect 4 (CFDate unwritable) → Task 4 ✅
- End-to-end proof → Task 5 ✅
- Misleading docs/skill → Task 6 ✅

**Type/name consistency:** `redump` method name matches across `RPCServer.swift` dispatch, `handleRedump`, `ax.py` `cmd_redump`/parser, README, and skill. `parseTimeOfDay(_:basedOn:)` and `rawAttr(_:_:)` are defined in Task 4 Step 3 and consumed in Task 4 Step 4. `interactiveRoles` strings (`AXDateTimeArea`, `AXDateField`, `AXTimeField`) match the role checks used in test asserts.

**Placeholder scan:** No TBD/TODO/"handle edge cases" — every code step shows full code; every test step shows the command and expected output.

**Known follow-ups (out of scope, do not expand here):**
- Ref-table growth: each `redump` mints fresh refs and never evicts. Fine for v1; revisit if sessions get long-lived.
- If `Delete`/edit affordances for an existing alarm aren't cleanly surfaced (Task 5 Step 3), that's a separate enhancement.
- AM/PM display vs. 24h: the driver writes a CFDate by UTC H:M; if a future picker interprets AXValue in local time, revisit `parseTimeOfDay`'s timezone choice.
