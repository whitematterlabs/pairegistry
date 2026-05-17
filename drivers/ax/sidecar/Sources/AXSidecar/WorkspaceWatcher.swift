import Foundation
import AppKit

/// Watches NSWorkspace.runningApplications and attaches an AppObserver
/// per macOS app. Posts ax:app_launched / ax:app_terminated on lifecycle.
///
/// One instance, owned by main.swift. Holds AppObservers in a dictionary
/// keyed by PID so they outlive their attach scope.
final class WorkspaceWatcher {
    private var observersByPID: [pid_t: AppObserver] = [:]
    private let queue = DispatchQueue(label: "ax.workspace")

    func start() {
        // Initial sweep.
        for app in NSWorkspace.shared.runningApplications {
            attach(app, emitLaunched: false)
        }

        let nc = NSWorkspace.shared.notificationCenter
        nc.addObserver(
            forName: NSWorkspace.didLaunchApplicationNotification,
            object: nil, queue: nil
        ) { [weak self] note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey]
                    as? NSRunningApplication else { return }
            self?.attach(app, emitLaunched: true)
        }

        nc.addObserver(
            forName: NSWorkspace.didTerminateApplicationNotification,
            object: nil, queue: nil
        ) { [weak self] note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey]
                    as? NSRunningApplication else { return }
            self?.detach(pid: app.processIdentifier,
                         bundleID: app.bundleIdentifier ?? "")
        }
    }

    var observedAppCount: Int {
        queue.sync { observersByPID.count }
    }

    private func attach(_ app: NSRunningApplication, emitLaunched: Bool) {
        // Skip our own process and background-only agents that have no AX
        // surface (no UI, no menu bar) — they'd just clutter the observer
        // pool with empty trees.
        let pid = app.processIdentifier
        if pid == ProcessInfo.processInfo.processIdentifier { return }
        if app.activationPolicy == .prohibited { return }

        let bundleID = app.bundleIdentifier ?? "unknown"
        let name = app.localizedName ?? bundleID

        queue.sync {
            if observersByPID[pid] != nil { return }
            guard let obs = AppObserver(pid: pid, bundleID: bundleID, appName: name) else {
                FileHandle.standardError.write(
                    Data("axd: could not build AppObserver for pid \(pid) (\(bundleID))\n".utf8))
                return
            }
            obs.start()
            observersByPID[pid] = obs

            if emitLaunched {
                NDJSONEmitter.shared.emit(
                    kind: "ax:app_launched",
                    pid: pid,
                    bundleID: bundleID,
                    payload: ["name": name]
                )
            }

            // If this is Notification Center, wire its special-case watcher
            // so we capture every banner from every app.
            if bundleID == "com.apple.notificationcenterui" {
                NotificationCenterWatcher.shared.register(pid: pid)
            }
        }
    }

    private func detach(pid: pid_t, bundleID: String) {
        queue.sync {
            guard let obs = observersByPID.removeValue(forKey: pid) else { return }
            obs.stop()
            NDJSONEmitter.shared.emit(
                kind: "ax:app_terminated",
                pid: pid,
                bundleID: bundleID
            )
        }
    }
}
