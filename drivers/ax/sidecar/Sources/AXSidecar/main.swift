import Foundation
import AppKit
import AXSwift

// MARK: - Accessibility grant check
//
// Without the Accessibility TCC grant nothing works. We prompt with the
// system dialog; if the user denies we exit cleanly with rc=78 (EX_CONFIG)
// so the Python supervisor surfaces the failure without crash-looping.

if !UIElement.isProcessTrusted(withPrompt: true) {
    FileHandle.standardError.write(
        Data("axd: Accessibility grant missing — exiting with rc=78\n".utf8))
    exit(78)
}

FileHandle.standardError.write(Data("axd: starting\n".utf8))

// MARK: - Signal handling

let sigtermSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigtermSrc.setEventHandler {
    FileHandle.standardError.write(Data("axd: SIGTERM received, exiting\n".utf8))
    exit(0)
}
sigtermSrc.resume()
signal(SIGTERM, SIG_IGN)

let sigintSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigintSrc.setEventHandler {
    FileHandle.standardError.write(Data("axd: SIGINT received, exiting\n".utf8))
    exit(0)
}
sigintSrc.resume()
signal(SIGINT, SIG_IGN)

// MARK: - Subsystems
//
// Order:
//   1. WorkspaceWatcher — listens for app termination so dead sessions
//      surface scope_lost before clients try to act on stale refs.
//   2. ForegroundWatcher — listens for app activation, computes per-
//      session foreground bit when sessions register themselves.
//   3. RPCServer — opens the Unix socket. Once this is up the `ax` bin
//      tool can connect.

WorkspaceWatcher.shared.start()
ForegroundWatcher.shared.start()

let paiRoot = ProcessInfo.processInfo.environment["PAI_ROOT"]
    ?? (NSHomeDirectory() + "/.pai")
let socketPath = paiRoot + "/var/run/ax/axd.sock"

let rpc = RPCServer(socketPath: socketPath)
do {
    try rpc.start()
} catch {
    FileHandle.standardError.write(
        Data("axd: RPC start failed: \(error) — exiting\n".utf8))
    exit(1)
}

// MARK: - Run loop
//
// AXObserver callbacks fire on the run loop the observer was registered
// against; AXSwift wires CFRunLoopGetMain() by default. Park the main
// thread in the AppKit run loop so observer callbacks land here.

NSApplication.shared.setActivationPolicy(.accessory)
NSApplication.shared.run()
