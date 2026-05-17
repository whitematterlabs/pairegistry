import Foundation
import ApplicationServices
import AXSwift

// Same SIGABRT trap as in AppObserver — AXSwift's generic attribute<T>
// force-casts the AnyObject AX returns. NotificationCenter banner subtrees
// have AXStaticText nodes whose AXValue is occasionally a number/date, so
// we read everything via the raw C API and try cast.
private func safeString(_ element: UIElement, _ key: String) -> String? {
    var value: AnyObject?
    let err = AXUIElementCopyAttributeValue(
        element.element, key as CFString, &value)
    guard err == .success, let v = value else { return nil }
    if let s = v as? String { return s }
    if let n = v as? NSNumber { return n.stringValue }
    return nil
}

/// Special-cases com.apple.notificationcenterui as a system-wide banner sink.
/// Per AX_PLAN §Notification reception channel 3: Notification Center is an
/// ordinary AX-visible app, so we attach the same observer machinery and
/// re-emit AXWindowCreated as ax:notification with title + body extracted.
///
/// The actual subscription is done by AppObserver (Notification Center is
/// just another PID in the workspace sweep); this class owns the banner
/// extraction shape and keeps a registry so AppObserver knows to route
/// AXWindowCreated through us instead of ax:window_created.
final class NotificationCenterWatcher {
    static let shared = NotificationCenterWatcher()

    private let queue = DispatchQueue(label: "ax.notifcenter")
    private var registeredPIDs: Set<pid_t> = []

    private init() {}

    func register(pid: pid_t) {
        queue.sync { _ = registeredPIDs.insert(pid) }
        FileHandle.standardError.write(
            Data("axd: notification center attached (pid \(pid))\n".utf8))
    }

    func isRegistered(pid: pid_t) -> Bool {
        queue.sync { registeredPIDs.contains(pid) }
    }

    /// Called by AppObserver when an AXWindowCreated fires on a registered
    /// Notification Center PID. Walks the banner subtree, pulls title/body,
    /// emits ax:notification.
    ///
    /// Notification Center's banner is a window whose direct children include
    /// AXStaticText nodes — typically [title, body, source-app, timestamp].
    /// Shape varies across macOS majors; we collect every AXStaticText under
    /// the window and concatenate. If shape changes meaningfully (macOS 16+),
    /// this becomes the place to add per-version selectors.
    static func handleBannerWindow(element: UIElement, sourcePID: pid_t) {
        guard Coalescer.shared.shouldEmit(kind: "ax:notification") else { return }

        let texts = collectStaticTexts(element, depth: 0, maxDepth: 6)
        let title = texts.first ?? ""
        let body = texts.dropFirst().joined(separator: " ")
        // Notification Center hides the originating app in subrole/identifier
        // depending on macOS version; AXTitle of the window is often the app.
        let sourceApp = safeString(element, "AXTitle") ?? ""

        NDJSONEmitter.shared.emit(
            kind: "ax:notification",
            payload: [
                "source_app": sourceApp,
                "title": title,
                "body": body,
                "timestamp": Date().timeIntervalSince1970,
            ]
        )
    }

    private static func collectStaticTexts(
        _ element: UIElement, depth: Int, maxDepth: Int
    ) -> [String] {
        if depth > maxDepth { return [] }
        var out: [String] = []
        let role = safeString(element, "AXRole")
        if role == "AXStaticText" {
            if let v = safeString(element, "AXValue"), !v.isEmpty {
                out.append(v)
            }
            if let t = safeString(element, "AXTitle"), !t.isEmpty {
                out.append(t)
            }
        }
        // Children are always [AXUIElement] when present; AXSwift's array
        // cast for this attribute is safe. Wrap in try? to swallow any
        // attributeUnsupported / cannotComplete from leaf nodes.
        if let children: [UIElement] = try? element.attribute(.children) {
            for c in children {
                out.append(contentsOf: collectStaticTexts(
                    c, depth: depth + 1, maxDepth: maxDepth))
            }
        }
        return out
    }
}
