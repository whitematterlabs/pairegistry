import Foundation
import AppKit
import AXSwift

/// Unix-domain-socket JSON-RPC server. Each connection speaks
/// line-delimited JSON. One request, one response (request_id echoed).
///
/// Methods:
///   attach(bundle_id, target_pid, launch_if_needed=true)
///   detach(session_id)
///   act(session_id, ref, action, args?)         // args optional, defaults {}
///   expand(session_id, ref)
///   redump(session_id)                          // re-read the current tree
///   list_sessions(target_pid?)
///
/// The socket lives at $PAI_ROOT/var/run/ax/axd.sock. We `unlink` first so
/// a stale socket from a previous run doesn't refuse to bind.
final class RPCServer {
    private let socketPath: String
    private var listenFD: Int32 = -1
    private var acceptSource: DispatchSourceRead?
    private let queue = DispatchQueue(label: "ax.rpc.accept")
    private let workerQueue = DispatchQueue(label: "ax.rpc.worker", attributes: .concurrent)

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func start() throws {
        let parent = (socketPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(
            atPath: parent, withIntermediateDirectories: true)

        unlink(socketPath)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        if fd < 0 { throw POSIXError(.EIO) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathLen = socketPath.utf8.count
        guard pathLen < MemoryLayout.size(ofValue: addr.sun_path) else {
            close(fd); throw POSIXError(.ENAMETOOLONG)
        }
        withUnsafeMutablePointer(to: &addr.sun_path.0) { dst in
            socketPath.withCString { src in
                _ = strncpy(dst, src, pathLen)
            }
        }
        var bindErr: Int32 = 0
        withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sap in
                bindErr = bind(fd, sap, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if bindErr != 0 { close(fd); throw POSIXError(.EADDRINUSE) }

        // 0o600 — owner only. Same trust as the PAI's home.
        chmod(socketPath, 0o600)

        if listen(fd, 16) != 0 { close(fd); throw POSIXError(.EIO) }

        listenFD = fd
        let src = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        src.setEventHandler { [weak self] in self?.acceptOne() }
        src.resume()
        acceptSource = src

        FileHandle.standardError.write(
            Data("axd: RPC listening on \(socketPath)\n".utf8))
    }

    func stop() {
        acceptSource?.cancel()
        if listenFD >= 0 { close(listenFD); listenFD = -1 }
        unlink(socketPath)
    }

    // MARK: - Accept

    private func acceptOne() {
        let cfd = accept(listenFD, nil, nil)
        if cfd < 0 { return }
        workerQueue.async { [weak self] in
            self?.handleClient(cfd)
        }
    }

    private func handleClient(_ fd: Int32) {
        defer { close(fd) }
        var buf = Data()
        let chunk = 4096
        var tmp = [UInt8](repeating: 0, count: chunk)
        while true {
            let n = read(fd, &tmp, chunk)
            if n <= 0 { return }
            buf.append(tmp, count: n)
            while let nl = buf.firstIndex(of: 0x0a) {
                let line = buf.subdata(in: 0..<nl)
                buf.removeSubrange(0...nl)
                processLine(line, fd: fd)
            }
        }
    }

    // MARK: - Dispatch

    private func processLine(_ data: Data, fd: Int32) {
        guard let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            sendError(fd, requestID: "", code: "EBADREQ", message: "invalid JSON")
            return
        }
        let requestID = (raw["request_id"] as? String) ?? ""
        let method = (raw["method"] as? String) ?? ""
        let params = (raw["params"] as? [String: Any]) ?? [:]

        switch method {
        case "attach":        handleAttach(params, requestID: requestID, fd: fd)
        case "detach":        handleDetach(params, requestID: requestID, fd: fd)
        case "act":           handleAct(params, requestID: requestID, fd: fd)
        case "expand":        handleExpand(params, requestID: requestID, fd: fd)
        case "redump":        handleRedump(params, requestID: requestID, fd: fd)
        case "list_sessions": handleListSessions(params, requestID: requestID, fd: fd)
        default:
            sendError(fd, requestID: requestID, code: "EMETHOD",
                      message: "unknown method \(method)")
        }
    }

    // MARK: - Handlers

    private func handleAttach(_ params: [String: Any], requestID: String, fd: Int32) {
        guard let bundleID = params["bundle_id"] as? String else {
            sendError(fd, requestID: requestID, code: "EBADARG", message: "bundle_id")
            return
        }
        guard let targetPID = params["target_pid"] as? Int else {
            sendError(fd, requestID: requestID, code: "EBADARG", message: "target_pid")
            return
        }
        let launch = (params["launch_if_needed"] as? Bool) ?? true
        // show_owner waives the foreground gate for owner-initiated tasks in
        // a visible app. Defaults false: background-only piloting stays the
        // norm.
        let showOwner = (params["show_owner"] as? Bool) ?? false

        let resolved: Launcher.Resolved
        switch Launcher.resolve(bundleID: bundleID, launchIfNeeded: launch) {
        case .success(let r): resolved = r
        case .failure(let e):
            sendError(fd, requestID: requestID, code: String(describing: e), message: bundleID)
            return
        }

        let result = SessionManager.shared.create(
            targetPID: targetPID,
            pid: resolved.pid,
            bundleID: resolved.bundleID,
            windowID: resolved.windowID,
            window: resolved.window,
            application: resolved.application,
            allowForeground: showOwner,
            onTreeChanged: { session, _ in
                // Coarse: re-walk and report as a single "changed" set. A
                // smarter delta would diff against last walk; sufficient
                // for v1.
                let snapshot = TreeExtractor.snapshot(session: session)
                NDJSONEmitter.shared.emit(
                    kind: "ax:tree_changed",
                    targetPID: session.targetPID,
                    extra: [
                        "session_id": session.id,
                        "delta": ["added": [], "removed": [], "changed": snapshot.tree],
                        "diagnostics": snapshot.diagnostics,
                    ]
                )
            },
            onWindowDestroyed: { session in
                SessionManager.shared.remove(id: session.id)
                NDJSONEmitter.shared.emit(
                    kind: "ax:scope_lost",
                    targetPID: session.targetPID,
                    extra: [
                        "session_id": session.id,
                        "reason": SessionManager.LossReason.windowClosed.rawValue,
                    ]
                )
            }
        )

        switch result {
        case .failure(let e):
            sendError(fd, requestID: requestID, code: String(describing: e),
                      message: "attach \(bundleID)")
        case .success(let session):
            ForegroundWatcher.shared.registerSession(session, application: resolved.application)
            let snapshot = TreeExtractor.snapshot(session: session)
            // Emit the scope_attached event on the bus (owning PAI gets it
            // via target_pid routing) and also return the tree inline in
            // the RPC response so the caller has it without waiting on the
            // kernel round-trip.
            let payload: [String: Any] = [
                "session_id": session.id,
                "pid": Int(session.pid),
                "bundle_id": session.bundleID,
                "window_id": session.windowID,
                "tree": snapshot.tree,
                "diagnostics": snapshot.diagnostics,
            ]
            NDJSONEmitter.shared.emit(
                kind: "ax:scope_attached",
                targetPID: session.targetPID,
                extra: payload
            )
            sendOK(fd, requestID: requestID, result: payload)
        }
    }

    private func handleDetach(_ params: [String: Any], requestID: String, fd: Int32) {
        guard let sid = params["session_id"] as? String else {
            sendError(fd, requestID: requestID, code: "EBADARG", message: "session_id")
            return
        }
        guard let s = SessionManager.shared.remove(id: sid) else {
            sendError(fd, requestID: requestID, code: "ENOSESSION", message: sid)
            return
        }
        NDJSONEmitter.shared.emit(
            kind: "ax:scope_lost",
            targetPID: s.targetPID,
            extra: ["session_id": s.id,
                    "reason": SessionManager.LossReason.detached.rawValue]
        )
        sendOK(fd, requestID: requestID, result: ["session_id": sid])
    }

    private func handleAct(_ params: [String: Any], requestID: String, fd: Int32) {
        guard let sid = params["session_id"] as? String,
              let session = SessionManager.shared.session(id: sid) else {
            sendError(fd, requestID: requestID, code: "ENOSESSION", message: "")
            return
        }
        guard let ref = params["ref"] as? Int else {
            sendError(fd, requestID: requestID, code: "EBADARG", message: "ref")
            return
        }
        let action = (params["action"] as? String) ?? "press"
        let args = (params["args"] as? [String: Any]) ?? [:]

        let result = Actuator.perform(session: session, ref: ref, action: action, args: args)
        switch result {
        case .success:
            let body: [String: Any] = [
                "session_id": sid, "request_id": requestID,
                "ok": true,
            ]
            NDJSONEmitter.shared.emit(
                kind: "ax:action_result",
                targetPID: session.targetPID,
                extra: body
            )
            sendOK(fd, requestID: requestID, result: body)
        case .failure(let err):
            let body: [String: Any] = [
                "session_id": sid, "request_id": requestID,
                "ok": false, "error": String(describing: err),
            ]
            NDJSONEmitter.shared.emit(
                kind: "ax:action_result",
                targetPID: session.targetPID,
                extra: body
            )
            sendError(fd, requestID: requestID, code: String(describing: err), message: "")
        }
    }

    private func handleExpand(_ params: [String: Any], requestID: String, fd: Int32) {
        guard let sid = params["session_id"] as? String,
              let session = SessionManager.shared.session(id: sid) else {
            sendError(fd, requestID: requestID, code: "ENOSESSION", message: "")
            return
        }
        guard let ref = params["ref"] as? Int else {
            sendError(fd, requestID: requestID, code: "EBADARG", message: "ref")
            return
        }
        let children = TreeExtractor.expand(session: session, ref: ref)
        sendOK(fd, requestID: requestID,
               result: ["session_id": sid, "ref": ref, "children": children])
    }

    private func handleRedump(_ params: [String: Any], requestID: String, fd: Int32) {
        guard let sid = params["session_id"] as? String,
              let session = SessionManager.shared.session(id: sid) else {
            sendError(fd, requestID: requestID, code: "ENOSESSION", message: "")
            return
        }
        // Re-walk session.window from scratch. Picks up sheets/popovers that
        // appeared since attach. allocRef is monotonic, so this mints fresh
        // refs for the current elements; older refs keep resolving.
        let snapshot = TreeExtractor.snapshot(session: session)
        sendOK(fd, requestID: requestID,
               result: [
                   "session_id": sid,
                   "tree": snapshot.tree,
                   "diagnostics": snapshot.diagnostics,
               ])
    }

    private func handleListSessions(_ params: [String: Any], requestID: String, fd: Int32) {
        let filterPID = params["target_pid"] as? Int
        let all = SessionManager.shared.allSessions()
        let filtered = filterPID.map { tp in all.filter { $0.targetPID == tp } } ?? all
        let summary: [[String: Any]] = filtered.map { s in
            [
                "session_id": s.id,
                "target_pid": s.targetPID,
                "pid": Int(s.pid),
                "bundle_id": s.bundleID,
                "window_id": s.windowID,
                "foreground": s.isForeground,
            ]
        }
        sendOK(fd, requestID: requestID, result: ["sessions": summary])
    }

    // MARK: - Wire

    private func sendOK(_ fd: Int32, requestID: String, result: [String: Any]) {
        var obj: [String: Any] = ["ok": true, "result": result]
        if !requestID.isEmpty { obj["request_id"] = requestID }
        send(fd, obj)
    }

    private func sendError(_ fd: Int32, requestID: String, code: String, message: String) {
        var obj: [String: Any] = ["ok": false, "error": code]
        if !requestID.isEmpty { obj["request_id"] = requestID }
        if !message.isEmpty { obj["message"] = message }
        send(fd, obj)
    }

    private func send(_ fd: Int32, _ obj: [String: Any]) {
        guard let data = try? JSONSerialization.data(
            withJSONObject: obj, options: [.fragmentsAllowed]
        ) else { return }
        var payload = data
        payload.append(0x0a)
        payload.withUnsafeBytes { bytes in
            _ = write(fd, bytes.baseAddress, payload.count)
        }
    }
}
