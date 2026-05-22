import Foundation
import ApplicationServices
import AXSwift

/// Static AX helpers shared across Session / TreeExtractor / Actuator.
///
/// In the ambient-sensor era this file owned per-app event observers and
/// 14 emitter methods. The piloting model collapses that: each Session
/// owns its own AXObserver scoped to one window. What survived from the
/// old AppObserver is the *safe attribute access pattern* (commit
/// 59749c7) — AXSwift's generic `attribute<T>` force-casts and traps on
/// NSNumber/NSURL where we asked for String — and the ancestor walker.
enum AXHelpers {

    /// Roles we keep when flattening the actionable surface. Everything
    /// else is a container or decoration and gets collapsed out.
    static let interactiveRoles: Set<String> = [
        "AXButton", "AXMenuButton", "AXPopUpButton",
        "AXTextField", "AXTextArea", "AXSearchField", "AXSecureTextField",
        "AXLink",
        "AXMenuItem", "AXMenuBarItem",
        "AXCheckBox", "AXRadioButton",
        "AXComboBox",
        "AXSlider", "AXIncrementor",
        "AXDateTimeArea", "AXDateField", "AXTimeField",
        "AXTabGroup",
        "AXDisclosureTriangle",
    ]

    /// Roles kept only when they appear as labels next to interactive
    /// elements. TreeExtractor decides per-context.
    static let labelRoles: Set<String> = [
        "AXStaticText", "AXImage",
    ]

    // MARK: - Safe attribute access

    static func rawStringAttr(_ element: UIElement, _ key: String) -> String? {
        var value: AnyObject?
        let err = AXUIElementCopyAttributeValue(
            element.element, key as CFString, &value)
        guard err == .success, let v = value else { return nil }
        if let s = v as? String { return s }
        if let u = v as? URL { return u.absoluteString }
        if let n = v as? NSNumber { return n.stringValue }
        if let b = v as? Bool { return b ? "true" : "false" }
        if let d = v as? Date {
            let f = ISO8601DateFormatter()
            // Render in the local zone so the H:M shown matches the picker's
            // on-screen wall-clock (Clock interprets the AXValue's instant in
            // local time). A UTC render would be off by the zone offset.
            f.timeZone = TimeZone.current
            return f.string(from: d)
        }
        return nil
    }

    static func stringAttr(_ element: UIElement, _ attr: Attribute) -> String? {
        return rawStringAttr(element, attr.rawValue)
    }

    static func boolAttr(_ element: UIElement, _ attr: Attribute) -> Bool? {
        var value: AnyObject?
        let err = AXUIElementCopyAttributeValue(
            element.element, attr.rawValue as CFString, &value)
        guard err == .success, let v = value else { return nil }
        if let b = v as? Bool { return b }
        if let n = v as? NSNumber { return n.boolValue }
        return nil
    }

    static func childrenAttr(_ element: UIElement) -> [UIElement] {
        var value: AnyObject?
        let err = AXUIElementCopyAttributeValue(
            element.element, kAXChildrenAttribute as CFString, &value)
        guard err == .success, let arr = value as? [AXUIElement] else { return [] }
        return arr.map { UIElement($0) }
    }

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
    /// evaluated in the local time zone — Clock interprets a date picker's
    /// CFDate AXValue in local time, so the H:M we set is the wall-clock the
    /// saved alarm shows (a UTC build would land off by the zone offset).
    /// The Calendar resolves the offset for `ref`'s own date, so seasonal
    /// DST is handled correctly. nil if unparseable or out of range.
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
        if am || pm {
            // A meridiem only makes sense on a 12-hour clock hour; reject
            // contradictions like "19:50 PM" or "15 AM" rather than
            // silently ignoring the AM/PM and setting the wrong alarm.
            guard (1...12).contains(h) else { return nil }
            if pm && h < 12 { h += 12 }
            if am && h == 12 { h = 0 }
        }
        guard (0...23).contains(h) else { return nil }

        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone.current
        var comp = cal.dateComponents([.year, .month, .day], from: ref)
        comp.hour = h; comp.minute = m; comp.second = 0
        return cal.date(from: comp)
    }

    static func elementText(_ element: UIElement) -> String {
        if let v = stringAttr(element, .value), !v.isEmpty { return truncated(v) }
        if let t = stringAttr(element, .title), !t.isEmpty { return truncated(t) }
        if let d = stringAttr(element, .description), !d.isEmpty { return truncated(d) }
        return ""
    }

    static func truncated(_ s: String, max: Int = 200) -> String {
        if s.count <= max { return s }
        return String(s.prefix(max)) + "…"
    }

    static func findAncestor(_ start: UIElement, role: String) -> UIElement? {
        var cursor: UIElement? = start
        var hops = 0
        while let cur = cursor, hops < 20 {
            if stringAttr(cur, .role) == role { return cur }
            cursor = (try? cur.attribute(.parent) as UIElement?) ?? nil
            hops += 1
        }
        return nil
    }

    /// Stable-ish identifier for a window — AXIdentifier if set, else
    /// title, else the AXUIElement's hash. AX windows do not expose a
    /// platform window number, so this is what we get.
    static func windowID(_ element: UIElement) -> String {
        if let id = stringAttr(element, .identifier), !id.isEmpty { return id }
        if let t = stringAttr(element, .title), !t.isEmpty { return "title:\(t)" }
        return "ax:\(String(UInt(bitPattern: ObjectIdentifier(element).hashValue), radix: 16))"
    }
}
