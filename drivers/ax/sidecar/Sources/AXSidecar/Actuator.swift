import Foundation
import ApplicationServices
import AXSwift

/// Perform actions against a session's elements. All actions go through
/// `perform`, which runs the pre-flight gate (background-only, not
/// paused, secure input not active) before touching AX.
enum Actuator {

    enum ActError: Error, CustomStringConvertible {
        case foreground            // scoped window is currently frontmost
        case paused                // ForegroundWatcher set isForeground=true
        case secureInput           // password / sudo / 1Password
        case refGone               // ref no longer resolves to an element
        case unsupportedAction(String)
        case axError(String)

        var description: String {
            switch self {
            case .foreground:            return "EFOREGROUND"
            case .paused:                return "EPAUSED"
            case .secureInput:           return "ESECUREINPUT"
            case .refGone:               return "EREFGONE"
            case .unsupportedAction(let a): return "EACTION:\(a)"
            case .axError(let s):        return "EAX:\(s)"
            }
        }
    }

    /// Generic dispatch. `args` shape depends on `action`:
    ///   - "press":          {}
    ///   - "set_value":      {"value": "..."}
    ///   - "show_menu":      {}
    ///   - "pick_menu_item": {"title": "..."}
    static func perform(session: Session,
                        ref: Int,
                        action: String,
                        args: [String: Any]) -> Result<Void, ActError> {
        // Pre-flight. Foreground gate is the headline rule; pause + secure
        // input cover the narrower cases where the gate isn't strict enough.
        // A session attached with show_owner waives the gate — the owner
        // asked PAI to act in the window they're looking at. Secure-input
        // gating (in set_value, below) still applies regardless.
        if session.isForeground && !session.allowForeground {
            return .failure(.foreground)
        }
        guard let element = session.element(forRef: ref) else {
            return .failure(.refGone)
        }

        switch action {
        case "press":
            return performPress(element)
        case "set_value":
            return performSetValue(element, args: args)
        case "show_menu":
            return performAction(element, kAXShowMenuAction)
        case "pick_menu_item":
            return performPickMenuItem(element, args: args)
        default:
            return .failure(.unsupportedAction(action))
        }
    }

    // MARK: - Concrete actions

    private static func performPress(_ element: UIElement) -> Result<Void, ActError> {
        return performAction(element, kAXPressAction)
    }

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

    private static func performAction(_ element: UIElement,
                                      _ action: String) -> Result<Void, ActError> {
        let err = AXUIElementPerformAction(element.element, action as CFString)
        if err == .success { return .success(()) }
        return .failure(.axError("\(action)=\(err.rawValue)"))
    }

    private static func performPickMenuItem(_ element: UIElement,
                                            args: [String: Any]) -> Result<Void, ActError> {
        guard let title = args["title"] as? String else {
            return .failure(.unsupportedAction("pick_menu_item:missing title"))
        }
        for child in AXHelpers.childrenAttr(element) {
            let role = AXHelpers.stringAttr(child, .role) ?? ""
            if role != "AXMenuItem" { continue }
            let t = AXHelpers.stringAttr(child, .title) ?? ""
            if t == title {
                return performAction(child, kAXPressAction)
            }
        }
        return .failure(.unsupportedAction("pick_menu_item:not found"))
    }
}
