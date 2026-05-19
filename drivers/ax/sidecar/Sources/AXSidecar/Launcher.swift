import Foundation
import AppKit
import AXSwift

/// Resolve a bundle ID to a running NSRunningApplication, optionally
/// launching it if absent, and wait for its main window to appear.
///
/// `attach` is the single-step entry point: caller hands us a bundle ID,
/// we return the (pid, AXSwift Application, main window) tuple ready for
/// SessionManager.create.
enum Launcher {

    enum LaunchError: Error, CustomStringConvertible {
        case bundleNotFound(String)
        case launchFailed(String)
        case noMainWindow(String)

        var description: String {
            switch self {
            case .bundleNotFound(let b): return "ENOTFOUND:\(b)"
            case .launchFailed(let s): return "ELAUNCH:\(s)"
            case .noMainWindow(let b): return "ENOWINDOW:\(b)"
            }
        }
    }

    struct Resolved {
        let pid: pid_t
        let bundleID: String
        let application: Application
        let window: UIElement
        let windowID: String
    }

    /// Synchronous resolve. Caller passes the timeout budget for app
    /// launch + main-window appearance. Runs on the calling queue.
    static func resolve(bundleID: String,
                        launchIfNeeded: Bool,
                        timeout: TimeInterval = 8.0) -> Result<Resolved, LaunchError> {
        if let running = findRunning(bundleID: bundleID) {
            return waitForMainWindow(app: running, timeout: timeout)
        }
        if !launchIfNeeded {
            return .failure(.bundleNotFound(bundleID))
        }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
            return .failure(.bundleNotFound(bundleID))
        }

        let config = NSWorkspace.OpenConfiguration()
        config.activates = false   // background — don't yank the owner's focus
        config.addsToRecentItems = false

        var launched: NSRunningApplication?
        var launchErr: Error?
        let sem = DispatchSemaphore(value: 0)
        NSWorkspace.shared.openApplication(at: url, configuration: config) { app, err in
            launched = app
            launchErr = err
            sem.signal()
        }
        if sem.wait(timeout: .now() + timeout) == .timedOut {
            return .failure(.launchFailed("timeout"))
        }
        if let err = launchErr {
            return .failure(.launchFailed(err.localizedDescription))
        }
        guard let app = launched else {
            return .failure(.launchFailed("nil app"))
        }
        return waitForMainWindow(app: app, timeout: timeout)
    }

    // MARK: - Helpers

    private static func findRunning(bundleID: String) -> NSRunningApplication? {
        return NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == bundleID }
    }

    private static func waitForMainWindow(app: NSRunningApplication,
                                          timeout: TimeInterval) -> Result<Resolved, LaunchError> {
        guard let axApp = Application(forProcessID: app.processIdentifier) else {
            return .failure(.launchFailed("no AX application for pid \(app.processIdentifier)"))
        }
        let bundleID = app.bundleIdentifier ?? "unknown"
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let win = (try? axApp.attribute(.mainWindow) as UIElement?) ?? nil {
                let wid = AXHelpers.windowID(win)
                return .success(Resolved(
                    pid: app.processIdentifier,
                    bundleID: bundleID,
                    application: axApp,
                    window: win,
                    windowID: wid
                ))
            }
            // Some apps populate AXWindows before AXMainWindow.
            if let arr = (try? axApp.attribute(.windows) as [UIElement]?) ?? nil,
               let first = arr.first {
                let wid = AXHelpers.windowID(first)
                return .success(Resolved(
                    pid: app.processIdentifier,
                    bundleID: bundleID,
                    application: axApp,
                    window: first,
                    windowID: wid
                ))
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return .failure(.noMainWindow(bundleID))
    }
}
