import Foundation
import ApplicationServices
import AXSwift

/// Per-PID AXObserver wrapper. Subscribes to the full Phase 1 notification
/// set on the application root; AX delivers child-element notifications up
/// to the application observer automatically.
///
/// One instance per running app. Held alive by WorkspaceWatcher.
final class AppObserver {
    let pid: pid_t
    let bundleID: String
    let appName: String

    private let application: Application
    private var observer: Observer?

    /// Notifications attached to the application root. Per-element
    /// AXValueChanged subscriptions are folded in via the app-wide observer:
    /// AXObserver delivers AXValueChanged from focused descendants to the
    /// root observer if we add the notification at the application level —
    /// which is what we do here.
    private static let notifications: [AXNotification] = [
        .focusedUIElementChanged,
        .focusedWindowChanged,
        .mainWindowChanged,
        .windowCreated,
        .uiElementDestroyed,
        .announcementRequested,
        .valueChanged,
        .selectedTextChanged,
        .titleChanged,
    ]

    // Track the last URL seen on this app's most recent AXWebArea.
    // UIElement isn't Hashable so we can't key per-web-area; an app rarely
    // has multiple focused web areas at once, so a single slot is fine for
    // Phase 1. If Phase 2 finds we miss URL changes in multi-tab Safari,
    // switch to keying by the AXWebArea's AXIdentifier.
    private var lastURL: String = ""

    init?(pid: pid_t, bundleID: String, appName: String) {
        guard let app = Application(forProcessID: pid) else {
            return nil
        }
        self.pid = pid
        self.bundleID = bundleID
        self.appName = appName
        self.application = app
    }

    func start() {
        let obs = application.createObserver { [weak self] (
            _: Observer, element: UIElement,
            notif: AXNotification, info: [String: AnyObject]?
        ) in
            self?.handle(element: element, notification: notif, info: info)
        }
        guard let obs = obs else {
            FileHandle.standardError.write(
                Data("axd: createObserver returned nil for pid \(pid) (\(bundleID))\n".utf8))
            return
        }
        self.observer = obs
        for n in Self.notifications {
            do {
                try obs.addNotification(n, forElement: application)
            } catch let err as AXError where err == .notificationUnsupported {
                continue
            } catch let err as AXError where err == .notificationAlreadyRegistered {
                continue
            } catch let err as AXError where err == .apiDisabled {
                NDJSONEmitter.shared.emit(
                    kind: "ax:permission_lost",
                    payload: ["grant": "Accessibility"]
                )
                return
            } catch {
                FileHandle.standardError.write(
                    Data("axd: addNotification \(n.rawValue) on pid \(pid) failed: \(error)\n".utf8))
            }
        }
    }

    func stop() {
        observer = nil
    }

    // MARK: - Dispatch

    private func handle(element: UIElement,
                        notification: AXNotification,
                        info: [String: AnyObject]?) {
        switch notification {
        case .focusedUIElementChanged:
            emitFocusChanged(element: element)
        case .focusedWindowChanged, .mainWindowChanged:
            emitWindowChanged(element: element)
        case .windowCreated:
            emitWindowCreated(element: element)
        case .uiElementDestroyed:
            emitElementDestroyed(element: element)
        case .announcementRequested:
            emitAnnouncement(element: element, info: info)
        case .valueChanged:
            emitValueChanged(element: element)
        case .selectedTextChanged, .titleChanged:
            // Phase 1: not in events.yaml. Subscribed for future phases.
            // selectedTextChanged: typing fires it constantly; we want
            // <50/sec sustained and the coalescer would cap it anyway.
            break
        default:
            break
        }
    }

    // MARK: - Emitters

    private func emitFocusChanged(element: UIElement) {
        guard Coalescer.shared.shouldEmit(kind: "ax:focus_changed") else { return }
        let role = stringAttr(element, .role) ?? ""
        let identifier = stringAttr(element, .identifier) ?? ""
        let text = elementText(element)
        let window = (try? element.attribute(.window) as UIElement?) ?? nil
        let windowTitle = window.flatMap { stringAttr($0, .title) } ?? ""

        // AXWebArea AXURL delta detection: the focused element on a web page
        // is often a descendant of the AXWebArea. Walk up to the nearest
        // AXWebArea and check its AXURL against our last-seen value.
        emitURLChangeIfNeeded(focused: element)

        NDJSONEmitter.shared.emit(
            kind: "ax:focus_changed",
            pid: pid,
            bundleID: bundleID,
            payload: [
                "window_title": windowTitle,
                "role": role,
                "identifier": identifier,
                "text": text,
            ]
        )
    }

    private func emitWindowChanged(element: UIElement) {
        guard Coalescer.shared.shouldEmit(kind: "ax:window_changed") else { return }
        let title = stringAttr(element, .title) ?? ""
        let frame = frameAttr(element)
        NDJSONEmitter.shared.emit(
            kind: "ax:window_changed",
            pid: pid,
            bundleID: bundleID,
            payload: [
                "window_id": elementID(element),
                "title": title,
                "frame": frame,
            ]
        )
    }

    private func emitWindowCreated(element: UIElement) {
        guard Coalescer.shared.shouldEmit(kind: "ax:window_created") else { return }
        // Notification Center is a system-wide notification sink; route its
        // window-created events through the notification path instead.
        if bundleID == "com.apple.notificationcenterui" {
            NotificationCenterWatcher.handleBannerWindow(element: element, sourcePID: pid)
            return
        }
        NDJSONEmitter.shared.emit(
            kind: "ax:window_created",
            pid: pid,
            bundleID: bundleID,
            payload: [
                "window_id": elementID(element),
                "title": stringAttr(element, .title) ?? "",
            ]
        )
    }

    private func emitElementDestroyed(element: UIElement) {
        let role = stringAttr(element, .role) ?? ""
        guard role == "AXWindow" else { return }
        guard Coalescer.shared.shouldEmit(kind: "ax:window_destroyed") else { return }
        NDJSONEmitter.shared.emit(
            kind: "ax:window_destroyed",
            pid: pid,
            bundleID: bundleID,
            payload: ["window_id": elementID(element)]
        )
    }

    private func emitAnnouncement(element: UIElement, info: [String: AnyObject]?) {
        guard Coalescer.shared.shouldEmit(kind: "ax:announcement") else { return }
        // Per Apple docs, AXAnnouncementRequested user info dict contains
        // kAXAnnouncementKey (NSString) and kAXPriorityKey (NSNumber).
        let text = (info?["AXAnnouncement"] as? String) ?? ""
        let prio = (info?["AXPriority"] as? Int).map(prioName) ?? "medium"
        NDJSONEmitter.shared.emit(
            kind: "ax:announcement",
            pid: pid,
            bundleID: bundleID,
            payload: ["text": text, "priority": prio]
        )
    }

    private func emitValueChanged(element: UIElement) {
        // Live-region detection: web/Electron toasts have AXARIALive set.
        // Route those to ax:live_region; everything else to ax:value_changed.
        let aria = rawStringAttr(element, "AXARIALive")
        let isLive = aria != nil && aria != "off" && aria != ""
        let role = stringAttr(element, .role) ?? ""

        if isLive {
            guard Coalescer.shared.shouldEmit(kind: "ax:live_region") else { return }
            NDJSONEmitter.shared.emit(
                kind: "ax:live_region",
                pid: pid,
                bundleID: bundleID,
                payload: [
                    "role": role,
                    "text": elementText(element),
                    "politeness": aria ?? "polite",
                ]
            )
            return
        }

        guard Coalescer.shared.shouldEmit(kind: "ax:value_changed") else { return }
        NDJSONEmitter.shared.emit(
            kind: "ax:value_changed",
            pid: pid,
            bundleID: bundleID,
            payload: [
                "role": role,
                "value": elementText(element),
            ]
        )
    }

    private func emitURLChangeIfNeeded(focused: UIElement) {
        guard let webArea = findAncestor(focused, role: "AXWebArea") else { return }
        let newURL = stringAttr(webArea, .url) ?? ""
        if newURL.isEmpty { return }
        if newURL == lastURL { return }
        let prev = lastURL
        lastURL = newURL
        guard Coalescer.shared.shouldEmit(kind: "ax:url_changed") else { return }
        NDJSONEmitter.shared.emit(
            kind: "ax:url_changed",
            pid: pid,
            bundleID: bundleID,
            payload: ["old_url": prev, "new_url": newURL]
        )
    }

    // MARK: - Attribute helpers

    private func rawStringAttr(_ element: UIElement, _ key: String) -> String? {
        do {
            if let s: String = try element.attribute(key) { return s }
        } catch {}
        return nil
    }

    private func stringAttr(_ element: UIElement, _ attr: Attribute) -> String? {
        do {
            if let s: String = try element.attribute(attr) { return s }
            if let v: AnyObject = try element.attribute(attr) {
                if let s = v as? String { return s }
                if let u = v as? URL { return u.absoluteString }
                return String(describing: v)
            }
        } catch {}
        return nil
    }

    private func elementText(_ element: UIElement) -> String {
        // Try AXValue first (text fields, sliders), then AXTitle, then AXDescription.
        if let v = stringAttr(element, .value), !v.isEmpty { return truncated(v) }
        if let t = stringAttr(element, .title), !t.isEmpty { return truncated(t) }
        if let d = stringAttr(element, .description), !d.isEmpty { return truncated(d) }
        return ""
    }

    private func truncated(_ s: String, max: Int = 200) -> String {
        if s.count <= max { return s }
        return String(s.prefix(max)) + "…"
    }

    private func frameAttr(_ element: UIElement) -> [Double] {
        do {
            if let pos: CGPoint = try element.attribute(.position),
               let size: CGSize = try element.attribute(.size) {
                return [Double(pos.x), Double(pos.y), Double(size.width), Double(size.height)]
            }
        } catch {}
        return []
    }

    private func elementID(_ element: UIElement) -> String {
        if let id = stringAttr(element, .identifier), !id.isEmpty { return id }
        if let t = stringAttr(element, .title), !t.isEmpty { return t }
        return "0x\(String(UInt(bitPattern: ObjectIdentifier(element).hashValue), radix: 16))"
    }

    private func findAncestor(_ start: UIElement, role: String) -> UIElement? {
        var cursor: UIElement? = start
        var hops = 0
        while let cur = cursor, hops < 20 {
            if stringAttr(cur, .role) == role { return cur }
            cursor = (try? cur.attribute(.parent) as UIElement?) ?? nil
            hops += 1
        }
        return nil
    }

    private func prioName(_ n: Int) -> String {
        switch n {
        case 90: return "high"
        case 50: return "medium"
        case 10: return "low"
        default: return "medium"
        }
    }
}
