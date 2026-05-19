import Foundation
import ApplicationServices
import AXSwift

/// One in-memory session, scoped to (pid, window). Owns the AXObserver for
/// the scoped app, a ref table mapping integer refs ↔ AXUIElements, and
/// the targetPID of the owning PAI (used to stamp every event the kernel
/// router needs to deliver point-to-point).
final class Session {
    let id: String
    let targetPID: Int
    let pid: pid_t
    let bundleID: String
    let windowID: String

    /// The AX window element this session is scoped to. May go nil-equivalent
    /// when the window is destroyed; that transition fires scope_lost.
    let window: UIElement

    /// AXSwift Application; held so the observer doesn't dangle.
    let application: Application

    /// The session's AXObserver. We only attach two notifications:
    ///   - .valueChanged → tree_changed (label/value mutations)
    ///   - .uiElementDestroyed → scope_lost when the *window* itself dies
    /// Heavier subscriptions live on the foreground watcher, not here.
    private var observer: Observer?

    /// {ref → AXUIElement}. Refs are stable across the session; the table
    /// grows monotonically. expand() and act() resolve refs via this map.
    private var refTable: [Int: UIElement] = [:]
    private var nextRef: Int = 1

    /// True when the scoped window is the frontmost window of the frontmost
    /// app. Set/cleared by ForegroundWatcher. Actuator refuses to act while
    /// foreground.
    var isForeground: Bool = false

    init(id: String,
         targetPID: Int,
         pid: pid_t,
         bundleID: String,
         windowID: String,
         window: UIElement,
         application: Application) {
        self.id = id
        self.targetPID = targetPID
        self.pid = pid
        self.bundleID = bundleID
        self.windowID = windowID
        self.window = window
        self.application = application
    }

    /// Wire the session's AXObserver to the scoped application. We attach
    /// at the application root so descendant valueChanged events propagate
    /// up. The handler closure filters out events outside the scoped window.
    func startObserving(onTreeChanged: @escaping (UIElement) -> Void,
                        onWindowDestroyed: @escaping () -> Void) {
        let obs = application.createObserver { [weak self] (
            _: Observer, element: UIElement,
            notif: AXNotification, _: [String: AnyObject]?
        ) in
            guard let self = self else { return }
            switch notif {
            case .valueChanged, .titleChanged:
                if self.isWithinScope(element) {
                    onTreeChanged(element)
                }
            case .uiElementDestroyed:
                // Window destruction surfaces as the window element itself
                // being destroyed. AXSwift's UIElement equality is by
                // underlying AXUIElement pointer.
                if element == self.window {
                    onWindowDestroyed()
                }
            default:
                break
            }
        }
        guard let obs = obs else {
            FileHandle.standardError.write(
                Data("axd: session \(id): createObserver failed\n".utf8))
            return
        }
        self.observer = obs

        for n: AXNotification in [.valueChanged, .titleChanged, .uiElementDestroyed] {
            do {
                try obs.addNotification(n, forElement: application)
            } catch {
                FileHandle.standardError.write(
                    Data("axd: session \(id): add \(n.rawValue) failed: \(error)\n".utf8))
            }
        }
    }

    func stopObserving() {
        observer = nil
    }

    // MARK: - Ref table

    func allocRef(_ element: UIElement) -> Int {
        let ref = nextRef
        nextRef += 1
        refTable[ref] = element
        return ref
    }

    func element(forRef ref: Int) -> UIElement? {
        return refTable[ref]
    }

    func allRefs() -> [(Int, UIElement)] {
        return refTable.sorted { $0.key < $1.key }.map { ($0.key, $0.value) }
    }

    // MARK: - Scope check

    /// True if `element` is `window` or a descendant. Walks up the parent
    /// chain; bounded to keep pathological trees safe.
    private func isWithinScope(_ element: UIElement) -> Bool {
        var cursor: UIElement? = element
        var hops = 0
        while let cur = cursor, hops < 40 {
            if cur == window { return true }
            cursor = (try? cur.attribute(.parent) as UIElement?) ?? nil
            hops += 1
        }
        return false
    }
}
