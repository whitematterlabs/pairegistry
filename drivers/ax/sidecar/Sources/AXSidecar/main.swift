import Foundation
import AppKit
import AXSwift

// MARK: - Accessibility grant check
//
// Phase 1 only needs the Accessibility TCC grant. On first start we prompt
// with the system dialog; if the user denies we emit ax:permission_lost and
// exit cleanly with rc=78 so the supervising Python driver can surface it
// without a crash loop. (rc=78 is EX_CONFIG from sysexits.h — "configuration
// error" — close enough semantically.)

if !UIElement.isProcessTrusted(withPrompt: true) {
    NDJSONEmitter.shared.emit(
        kind: "ax:permission_lost",
        payload: ["grant": "Accessibility"]
    )
    // Give the NDJSONEmitter queue a moment to flush before exiting.
    Thread.sleep(forTimeInterval: 0.5)
    FileHandle.standardError.write(
        Data("axd: Accessibility grant missing — exiting with rc=78\n".utf8))
    exit(78)
}

FileHandle.standardError.write(Data("axd: starting\n".utf8))

// MARK: - Signal handling
//
// SIGTERM (Python's terminate()) and SIGINT (Ctrl-C in dev). NSRunLoop won't
// service signal handlers, so we install dispatch sources on the main queue
// and call exit() from there.

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

// MARK: - RPC reader stub (Phase 2 surface)
//
// Phase 2 will multiplex JSON RPC requests over stdin. For Phase 1 we drain
// stdin so a closed pipe doesn't accumulate buffers, but we don't parse or
// dispatch anything. A line of `{"shutdown":true}` is honored as a courtesy
// (lets the Python supervisor request a clean exit without SIGTERM during
// dev workflows).

DispatchQueue.global(qos: .utility).async {
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.contains("\"shutdown\"") && trimmed.contains("true") {
            FileHandle.standardError.write(
                Data("axd: shutdown requested via stdin\n".utf8))
            exit(0)
        }
        // Future: parse JSON, route to RPCDispatcher.
    }
}

// MARK: - Subsystems

let secureInput = SecureInputPoller()
secureInput.start()

let watcher = WorkspaceWatcher()
watcher.start()
FileHandle.standardError.write(
    Data("axd: sidecar attached, \(watcher.observedAppCount) apps observed\n".utf8))

// MARK: - Run loop
//
// AXObserver delivers callbacks on whichever run loop the observer is
// registered against; AXSwift.createObserver wires CFRunLoopGetMain() by
// default. We park the main thread in the AppKit run loop so all observer
// callbacks fire here.

NSApplication.shared.setActivationPolicy(.accessory)
NSApplication.shared.run()
