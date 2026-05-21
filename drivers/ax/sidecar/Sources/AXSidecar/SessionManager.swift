import Foundation
import AppKit
import AXSwift

/// Owns all live sessions. Single source of truth for {session_id → Session}
/// and the reverse index {(pid, window_id) → session_id} used to enforce
/// scope uniqueness.
///
/// Thread safety: every public method synchronizes on `queue`. Callers
/// (RPCServer, ForegroundWatcher, WorkspaceWatcher) may come from any
/// queue.
final class SessionManager {
    static let shared = SessionManager()

    private let queue = DispatchQueue(label: "ax.sessions")
    private var sessions: [String: Session] = [:]
    private var byScope: [String: String] = [:]   // "pid:windowID" → sessionID
    private var byPID: [pid_t: Set<String>] = [:] // pid → {sessionIDs}
    private var nextID: Int = 1

    private init() {}

    private func scopeKey(_ pid: pid_t, _ windowID: String) -> String {
        return "\(pid):\(windowID)"
    }

    // MARK: - Attach

    enum AttachError: Error, CustomStringConvertible {
        case duplicate
        case launchFailed(String)
        case noMainWindow

        var description: String {
            switch self {
            case .duplicate: return "EDUPSCOPE"
            case .launchFailed(let s): return "ELAUNCH:\(s)"
            case .noMainWindow: return "ENOWINDOW"
            }
        }
    }

    /// Create + register a session. Caller (RPCServer) supplies the
    /// resolved application + window. Returns the new session or
    /// `.duplicate` if a session for this scope already exists.
    func create(targetPID: Int,
                pid: pid_t,
                bundleID: String,
                windowID: String,
                window: UIElement,
                application: Application,
                allowForeground: Bool = false,
                onTreeChanged: @escaping (Session, UIElement) -> Void,
                onWindowDestroyed: @escaping (Session) -> Void) -> Result<Session, AttachError> {
        return queue.sync {
            let key = scopeKey(pid, windowID)
            if byScope[key] != nil {
                return .failure(.duplicate)
            }
            let id = "s\(nextID)"
            nextID += 1
            let session = Session(
                id: id, targetPID: targetPID, pid: pid,
                bundleID: bundleID, windowID: windowID,
                window: window, application: application,
                allowForeground: allowForeground
            )
            sessions[id] = session
            byScope[key] = id
            byPID[pid, default: []].insert(id)
            session.startObserving(
                onTreeChanged: { [weak session] el in
                    guard let s = session else { return }
                    onTreeChanged(s, el)
                },
                onWindowDestroyed: { [weak session] in
                    guard let s = session else { return }
                    onWindowDestroyed(s)
                }
            )
            return .success(session)
        }
    }

    // MARK: - Lookup

    func session(id: String) -> Session? {
        return queue.sync { sessions[id] }
    }

    func sessions(forPID pid: pid_t) -> [Session] {
        return queue.sync {
            (byPID[pid] ?? []).compactMap { sessions[$0] }
        }
    }

    func allSessions() -> [Session] {
        return queue.sync { Array(sessions.values) }
    }

    // MARK: - Tear-down

    enum LossReason: String {
        case windowClosed = "window_closed"
        case appTerminated = "app_terminated"
        case grantRevoked = "grant_revoked"
        case detached
    }

    /// Remove a session and stop its observer. Idempotent.
    @discardableResult
    func remove(id: String) -> Session? {
        return queue.sync {
            guard let s = sessions.removeValue(forKey: id) else { return nil }
            s.stopObserving()
            byScope.removeValue(forKey: scopeKey(s.pid, s.windowID))
            if var set = byPID[s.pid] {
                set.remove(id)
                if set.isEmpty { byPID.removeValue(forKey: s.pid) }
                else { byPID[s.pid] = set }
            }
            return s
        }
    }

    /// Remove every session for `pid` — used when an app terminates.
    func removeAll(forPID pid: pid_t) -> [Session] {
        let ids = queue.sync { Array(byPID[pid] ?? []) }
        return ids.compactMap { remove(id: $0) }
    }
}
