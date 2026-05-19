import Foundation
import AppKit
import AXSwift

/// Tracks which app is frontmost (`NSWorkspace.didActivateApplication`) and
/// which window is main inside scoped apps (`AXMainWindowChanged`). When a
/// session's scoped window transitions to/from "frontmost window of
/// frontmost app", emits `ax:scope_lost {reason: paused|resumed}` to the
/// owning PAI.
///
/// Pause/resume are observation-only; the session keeps observing. Actuator
/// reads `session.isForeground` to gate actions.
final class ForegroundWatcher {
    static let shared = ForegroundWatcher()

    /// Per-app AXObserver, only created for apps that have at least one
    /// active session. Keyed by pid.
    private var appObservers: [pid_t: Observer] = [:]
    private let queue = DispatchQueue(label: "ax.foreground")

    private var currentFrontPID: pid_t = 0

    private init() {}

    /// Wire NSWorkspace activation notifications. Call once from main.swift.
    func start() {
        currentFrontPID = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0

        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil, queue: nil
        ) { [weak self] note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey]
                    as? NSRunningApplication else { return }
            self?.handleAppActivation(pid: app.processIdentifier)
        }
    }

    /// Called by RPCServer when a new session attaches. Ensures we have an
    /// AXMainWindowChanged observer on that app and runs an initial
    /// foreground check.
    func registerSession(_ session: Session, application: Application) {
        queue.sync {
            if appObservers[session.pid] == nil {
                let pid = session.pid
                let obs = application.createObserver { [weak self] (_, _, notif, _) in
                    if notif == .mainWindowChanged {
                        self?.recomputeForPID(pid)
                    }
                }
                if let obs = obs {
                    do {
                        try obs.addNotification(.mainWindowChanged, forElement: application)
                        appObservers[pid] = obs
                    } catch {
                        FileHandle.standardError.write(
                            Data("axd: foreground watcher add notif failed pid \(pid): \(error)\n".utf8))
                    }
                }
            }
        }
        // Initial state check.
        recomputeForPID(session.pid)
    }

    func unregisterApp(pid: pid_t) {
        queue.sync { _ = appObservers.removeValue(forKey: pid) }
    }

    // MARK: - Activation handling

    private func handleAppActivation(pid: pid_t) {
        let previousFront = currentFrontPID
        currentFrontPID = pid
        // Any session whose pid is either the previous or new frontmost may
        // have flipped state — recompute both.
        if previousFront != 0 { recomputeForPID(previousFront) }
        recomputeForPID(pid)
    }

    /// For every session of `pid`, compute the latest foreground bit and
    /// emit paused/resumed events on transition.
    private func recomputeForPID(_ pid: pid_t) {
        let sessions = SessionManager.shared.sessions(forPID: pid)
        if sessions.isEmpty { return }

        let isFront = (pid == currentFrontPID)
        for session in sessions {
            let scopedIsMain: Bool
            if isFront {
                let main = (try? session.application.attribute(.mainWindow) as UIElement?) ?? nil
                scopedIsMain = (main == session.window)
            } else {
                scopedIsMain = false
            }

            let wasForeground = session.isForeground
            if scopedIsMain != wasForeground {
                session.isForeground = scopedIsMain
                let reason = scopedIsMain ? "paused" : "resumed"
                NDJSONEmitter.shared.emit(
                    kind: "ax:scope_lost",
                    targetPID: session.targetPID,
                    extra: ["session_id": session.id, "reason": reason]
                )
            }
        }
    }
}
