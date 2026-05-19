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
