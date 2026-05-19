import Foundation
import AppKit

/// Tracks NSWorkspace.didTerminateApplication only — that's the single
/// system-wide signal we still need: when an app dies, every session for
/// that pid must surface `ax:scope_lost`.
///
/// In the ambient-sensor model this file built an AppObserver per running
/// app and emitted app_launched/app_terminated/window_changed/... for
/// every app on the system. None of that survives: PAIs only see events
/// for sessions they attached, and session-internal AX subscriptions live
/// inside Session.startObserving.
final class WorkspaceWatcher {
    static let shared = WorkspaceWatcher()

    private init() {}

    func start() {
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didTerminateApplicationNotification,
            object: nil, queue: nil
        ) { note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey]
                    as? NSRunningApplication else { return }
            let pid = app.processIdentifier
            let lost = SessionManager.shared.removeAll(forPID: pid)
            for s in lost {
                NDJSONEmitter.shared.emit(
                    kind: "ax:scope_lost",
                    targetPID: s.targetPID,
                    extra: [
                        "session_id": s.id,
                        "reason": SessionManager.LossReason.appTerminated.rawValue,
                    ]
                )
            }
            ForegroundWatcher.shared.unregisterApp(pid: pid)
        }
    }
}
